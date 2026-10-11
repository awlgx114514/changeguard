from pathlib import PurePath

from .action import Action, OperationKind


class ActionValidationError(ValueError):
    """Raised when an Action first violates a canonical operation contract."""


class ActionValidator:
    """Explicit semantic validation for Action V2; policy remains outside this boundary."""

    def __init__(self, execution_mode: str = "NATIVE"):
        self._execution_mode = str(execution_mode).upper()

    def validate(self, action: Action) -> None:
        if not isinstance(action, Action):
            raise ActionValidationError("decision must be an Action")
        if not action.id:
            raise ActionValidationError("Action id must not be empty")
        kind, fields = action.operation.kind, action.operation.fields
        if kind in {OperationKind.FILE_READ, OperationKind.FILE_REPLACE,
                    OperationKind.FILE_WRITE, OperationKind.FILE_CREATE}:
            self._path(fields.get("path"))
        if kind is OperationKind.FILE_FIND:
            self._text(fields.get("name_query"), "file.find requires name_query")
            if fields.get("directory") is not None:
                self._path(fields["directory"])
        if kind is OperationKind.FILE_SEARCH:
            self._text(fields.get("query"), "file.search requires query")
            if fields.get("path") is not None:
                self._path(fields["path"])
        if kind is OperationKind.FILE_READ:
            cursor = fields.get("cursor")
            ranged = "start_line" in fields or "line_count" in fields
            if cursor is not None and ranged:
                raise ActionValidationError("file.read cursor cannot be combined with a line range")
            if cursor is not None:
                self._text(cursor, "file.read cursor must be non-empty")
            if "start_line" in fields and (not isinstance(fields["start_line"], int) or fields["start_line"] < 1):
                raise ActionValidationError("file.read start_line must be positive")
            if "line_count" in fields and (not isinstance(fields["line_count"], int) or fields["line_count"] < 1):
                raise ActionValidationError("file.read line_count must be positive")
        if kind is OperationKind.FILE_REPLACE:
            self._text(fields.get("expected_text"), "file.replace requires expected_text")
            if not isinstance(fields.get("replacement"), str):
                raise ActionValidationError("file.replace requires text replacement")
            self._text(fields.get("observed_file_hash"), "file.replace requires observed_file_hash")
        if kind is OperationKind.FILE_WRITE:
            if not isinstance(fields.get("content"), str):
                raise ActionValidationError("file.write requires text content")
            self._text(fields.get("observed_file_hash"), "file.write requires observed_file_hash")
        if kind is OperationKind.FILE_CREATE and not isinstance(fields.get("content"), str):
            raise ActionValidationError("file.create requires text content")
        if kind is OperationKind.COMMAND_RUN:
            argv = fields.get("argv")
            if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(item, str) and item for item in argv):
                raise ActionValidationError("command.run requires a non-empty argv")
        if kind is OperationKind.PTC_RUN:
            if self._execution_mode != "PTC":
                raise ActionValidationError("ptc.run requires explicit PTC execution mode")
            self._text(fields.get("program"), "ptc.run requires program")

    @staticmethod
    def _text(value, message: str) -> None:
        if not isinstance(value, str) or not value:
            raise ActionValidationError(message)

    @classmethod
    def _path(cls, value) -> None:
        cls._text(value, "file path must be workspace-relative")
        path = PurePath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ActionValidationError("file path must be workspace-relative and remain inside the workspace")
