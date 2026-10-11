from typing import Dict, Optional, Tuple

from code_governance.contract.schema import ChangeContract
from code_governance.git.context import ChangeContext

from .core import Finding


class UnexpectedScopeChangeRule:
    name = "unexpected_scope_change"

    def check(self, contract: ChangeContract, context: ChangeContext) -> Tuple[Finding, ...]:
        expected = tuple(contract.scope.paths)
        return tuple(
            Finding(
                rule=self.name,
                code="unexpected_scope_change",
                message=f"Changed file is outside expected Contract scope: {change.path}",
                path=change.path,
            )
            for change in context.files
            if expected and not any(_path_in_scope(change.path, path) for path in expected)
        )


class UnexpectedFileRule(UnexpectedScopeChangeRule):
    """Compatibility import name for callers that have not migrated yet."""


class ForbiddenFileRule:
    name = "forbidden_file"

    def check(self, contract: ChangeContract, context: ChangeContext) -> Tuple[Finding, ...]:
        forbidden = set(contract.constraints.forbidden)
        return tuple(
            Finding(
                rule=self.name,
                code="forbidden_file",
                message=f"Changed file is forbidden: {change.path}",
                path=change.path,
                severity="error",
            )
            for change in context.files
            if change.path in forbidden
        )


class DiffSizeRule:
    name = "diff_size"
    # (maximum changed files, maximum added+deleted lines); None means unlimited.
    LIMITS: Dict[str, Tuple[Optional[int], Optional[int]]] = {
        "L0": (0, 0),
        "L1": (5, 200),
        "L2": (20, 1000),
        "L3": (None, None),
    }

    def check(self, contract: ChangeContract, context: ChangeContext) -> Tuple[Finding, ...]:
        max_files, max_lines = self.LIMITS[contract.scope.change_level]
        file_count = len(context.files)
        line_count = context.additions + context.deletions
        file_over = max_files is not None and file_count > max_files
        line_over = max_lines is not None and line_count > max_lines
        if not file_over and not line_over:
            return ()

        limit_details = []
        if file_over:
            limit_details.append(f"{file_count} files exceeds limit {max_files}")
        if line_over:
            limit_details.append(f"{line_count} lines exceeds limit {max_lines}")
        return (
            Finding(
                rule=self.name,
                code="diff_size_exceeded",
                message="; ".join(limit_details),
            ),
        )


def _path_in_scope(changed_path: str, scope_path: str) -> bool:
    normalized = scope_path.rstrip("/")
    return changed_path == normalized or changed_path.startswith(normalized + "/")
