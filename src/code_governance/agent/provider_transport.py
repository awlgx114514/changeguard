"""Model-facing transport contract and deterministic normalization to Action V2."""

from __future__ import annotations

from typing import Any, Mapping

from code_governance.runtime.action import Action, OperationKind


class ProviderTransportError(ValueError):
    """A provider payload that cannot become one canonical Action."""

    def __init__(self, category: str, detail: str):
        super().__init__(f"{category}: {detail}")
        self.category = category


def provider_transport_schema(*, include_run_code: bool = True) -> dict:
    """Return a provider-compatible envelope distinct from Action V2."""
    kinds = tuple(
        kind for kind in OperationKind
        if include_run_code or kind is not OperationKind.PTC_RUN
    )
    return {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": [kind.value for kind in kinds]},
            "arguments": {
                "anyOf": [_argument_schema(kind) for kind in kinds],
            },
            "reason": {"type": "string"},
        },
        "required": ["operation", "arguments", "reason"],
        "additionalProperties": False,
    }


def normalize_provider_transport(value: Mapping[str, Any], *, state=None) -> Action:
    """Validate explicit model intent and create exactly one Action V2."""
    if not isinstance(value, Mapping):
        raise ProviderTransportError("PROVIDER_TRANSPORT_INVALID", "payload must be an object")
    unexpected = set(value) - {"operation", "arguments", "reason"}
    if unexpected:
        raise ProviderTransportError(
            "PROVIDER_TRANSPORT_INVALID",
            f"unknown top-level fields: {', '.join(sorted(unexpected))}",
        )
    operation = value.get("operation")
    try:
        kind = OperationKind(operation)
    except (TypeError, ValueError) as error:
        raise ProviderTransportError("PROVIDER_TRANSPORT_INVALID", "operation is unknown") from error
    arguments = value.get("arguments")
    if not isinstance(arguments, Mapping):
        raise ProviderTransportError("OPERATION_ARGUMENT_INVALID", "arguments must be an object")
    reason = value.get("reason")
    if not isinstance(reason, str):
        raise ProviderTransportError("PROVIDER_TRANSPORT_INVALID", "reason must be text")

    catalog = Action.provider_arguments(kind)
    declared = {name for name, _, _, _ in catalog}
    unexpected_arguments = set(arguments) - declared
    if unexpected_arguments:
        raise ProviderTransportError(
            "OPERATION_ARGUMENT_INVALID",
            f"fields do not belong to {kind.value}: {', '.join(sorted(unexpected_arguments))}",
        )
    fields = {}
    for name, canonical_name, field_type, required in catalog:
        supplied = arguments.get(name)
        if required and supplied is None:
            raise ProviderTransportError(
                "OPERATION_ARGUMENT_INVALID", f"{kind.value} requires {name}"
            )
        if supplied is None:
            continue
        if field_type == "string" and not isinstance(supplied, str):
            raise ProviderTransportError(
                "OPERATION_ARGUMENT_INVALID", f"{kind.value} {name} must be text"
            )
        if field_type == "integer" and (not isinstance(supplied, int) or isinstance(supplied, bool)):
            raise ProviderTransportError(
                "OPERATION_ARGUMENT_INVALID", f"{kind.value} {name} must be an integer"
            )
        if field_type == "array" and (
            not isinstance(supplied, list) or not all(isinstance(item, str) for item in supplied)
        ):
            raise ProviderTransportError(
                "OPERATION_ARGUMENT_INVALID", f"{kind.value} {name} must be a string array"
            )
        fields[canonical_name] = supplied
    if kind in {OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE}:
        observed_hash = _current_read_hash(state, fields.get("path"))
        if observed_hash is not None:
            fields["observed_file_hash"] = observed_hash
    return Action.create_operation(kind, reason=reason, **fields)


def _argument_schema(kind: OperationKind) -> dict:
    fields = Action.provider_arguments(kind)
    guidance = Action.agent_guidance(kind)
    properties = {
        name: _field_schema(field_type, required)
        for name, _, field_type, required in fields
    }
    return {
        "type": "object",
        "title": kind.value,
        "description": (
            f"{guidance['description']} "
            f"Use when: {', '.join(guidance['when_to_use']) or 'the operation applies'}. "
            f"Do not use when: {', '.join(guidance['when_not_to_use']) or 'no boundary is declared'}"
        ),
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _field_schema(field_type: str, required: bool) -> dict:
    if field_type == "array":
        schema = {"type": "array", "items": {"type": "string"}}
    else:
        schema = {"type": field_type}
    if not required:
        schema["type"] = [schema["type"], "null"]
    return schema


def _current_read_hash(state, path):
    if state is None or not isinstance(path, str):
        return None
    history = zip(
        reversed(getattr(state, "recent_actions", ())),
        reversed(getattr(state, "recent_observations", ())),
    )
    for action, observation in history:
        if (
            action.operation.kind is OperationKind.FILE_READ
            and action.operation.fields.get("path") == path
            and observation.success
            and isinstance(observation.metadata.get("file_hash"), str)
        ):
            return observation.metadata["file_hash"]
    return None
