from dataclasses import replace
from pathlib import Path

from .action import Action, Operation, OperationKind


class TargetResolutionError(ValueError):
    stage = "TARGET_RESOLUTION"


class TargetResolver:
    """Normalize only declared V2 path fields to workspace-relative identities."""

    _PATH_FIELDS = {
        OperationKind.FILE_READ: ("path",),
        OperationKind.FILE_SEARCH: ("path",),
        OperationKind.FILE_REPLACE: ("path",),
        OperationKind.FILE_WRITE: ("path",),
        OperationKind.FILE_CREATE: ("path",),
        OperationKind.FILE_FIND: ("directory",),
    }

    def resolve(self, action: Action, workspace: Path) -> Action:
        fields = dict(action.operation.fields)
        root = Path(workspace).resolve()
        for field in self._PATH_FIELDS.get(action.operation.kind, ()):
            raw = fields.get(field)
            if raw is None:
                continue
            candidate = (root / Path(raw)).resolve()
            try:
                canonical = candidate.relative_to(root).as_posix()
            except ValueError as error:
                raise TargetResolutionError(f"{field} must remain inside the workspace") from error
            if not canonical or canonical == ".":
                raise TargetResolutionError(f"{field} must name a workspace file")
            fields[field] = canonical
        return replace(action, operation=Operation(action.operation.kind, fields))
