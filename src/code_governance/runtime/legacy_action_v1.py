"""One-way Action V1 compatibility ingress.

This is intentionally not used by model adapters or Runtime execution.  It is
only available to explicitly migrate persisted historical payloads.  Ambiguous
unsafe V1 writes are rejected instead of being guessed into a V2 write mode.
"""

from typing import Any, Mapping
import uuid

from .action import Action, ActionType, Operation, OperationKind


class LegacyActionV1Normalizer:
    """Convert an unambiguous historical payload once, or reject it."""

    @staticmethod
    def normalize(value: Mapping[str, Any]) -> Action:
        try:
            action_type = ActionType(value.get("action_type") or value["type"])
        except (KeyError, ValueError) as error:
            raise ValueError("legacy action_type is unsupported") from error
        fields = dict(value.get("input", {}) or {})
        target = str(value.get("target", ""))
        if action_type is ActionType.READ:
            operation = (
                Operation.create(OperationKind.FILE_FIND, name_query=target or fields.get("query"))
                if fields.get("action") == "search_file"
                else Operation.create(OperationKind.FILE_READ, path=target)
            )
        elif action_type is ActionType.WRITE:
            # Deliberately normalize only to the strict existing-file write.
            # Missing V2 provenance remains a validator error; the normalizer
            # must not guess whether this was a create or local replacement.
            operation = Operation.create(OperationKind.FILE_WRITE, path=target, content=fields.get("content"))
        elif action_type is ActionType.RUN_COMMAND:
            operation = Operation.create(OperationKind.COMMAND_RUN, argv=fields.get("command"))
        elif action_type is ActionType.GIT_OPERATION:
            operation = Operation.create(
                OperationKind.GIT_DIFF if (target or fields.get("action")) == "diff" else OperationKind.GIT_STATUS
            )
        elif action_type is ActionType.RUN_CODE:
            operation = Operation.create(OperationKind.PTC_RUN, program=fields.get("program"))
        elif action_type is ActionType.GET_CONTEXT:
            operation = Operation.create(OperationKind.CONTEXT_CURRENT)
        else:
            operation = Operation.create(OperationKind.WORKFLOW_FINISH)
        return Action(id=str(value.get("id") or uuid.uuid4().hex), operation=operation, reason=str(value.get("reason", "")))
