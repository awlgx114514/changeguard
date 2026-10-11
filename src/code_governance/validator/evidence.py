"""Immutable factual inputs for deterministic validation.

This module deliberately stores the Runtime facts it receives.  It does not
reinterpret command output or make validation decisions; that remains the
responsibility of :mod:`code_governance.validator.core`.
"""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess
from typing import Mapping, Tuple, Union

from code_governance.git.context import ChangeContext, GitContextError, collect_context
from code_governance.runtime.execution import ExecutionResult


class ValidationEvidenceError(ValueError):
    """Raised when supplied validation facts cannot form a coherent lineage."""


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """The change facts and deterministic identity of one workspace instant."""

    repository: str
    revision: str
    fingerprint: str
    change_context: ChangeContext


@dataclass(frozen=True)
class WorkspaceIdentity:
    """The workspace identities observed immediately before and after verification."""

    repository: str
    revision_before: str
    revision_after: str
    fingerprint_before: str
    fingerprint_after: str

    @property
    def is_consistent(self) -> bool:
        return (
            self.revision_before == self.revision_after
            and self.fingerprint_before == self.fingerprint_after
        )


@dataclass(frozen=True)
class VerificationEvidence:
    """One VerificationClaim's full, fact-preserving execution lineage."""

    claim_id: str
    request_id: str
    action_id: str
    execution_result_id: str
    argv: Tuple[str, ...]
    expected_result: Mapping[str, object]
    actual_result: Mapping[str, object]
    passed: bool
    execution_result: ExecutionResult

    @property
    def execution_failed(self) -> bool:
        return not self.execution_result.success

    @property
    def verification_failed(self) -> bool:
        """A command ran successfully but did not prove its declared claim."""
        return not self.execution_failed and not self.passed

    @classmethod
    def from_execution(cls, execution) -> "VerificationEvidence":
        result = execution.result
        actual = {}
        if "returncode" in result.metadata:
            actual["returncode"] = result.metadata["returncode"]
        return cls(
            claim_id=execution.claim_id,
            request_id=execution.request_id,
            action_id=execution.action_id,
            # ExecutionResult deliberately has no separate result identifier;
            # action_id is its stable one-to-one correlation identity.
            execution_result_id=result.action_id,
            argv=execution.request.argv,
            expected_result={"exit_code": execution.claim.expected_result.exit_code},
            actual_result=actual,
            passed=execution.passed,
            execution_result=result,
        )


@dataclass(frozen=True)
class ValidationEvidence:
    """The only factual input accepted by the Validator boundary."""

    workspace: WorkspaceIdentity
    change_context: ChangeContext
    execution_results: Tuple[ExecutionResult, ...]
    verification: Tuple[VerificationEvidence, ...]

    def __post_init__(self) -> None:
        execution_ids = {result.action_id for result in self.execution_results}
        for evidence in self.verification:
            if evidence.execution_result_id not in execution_ids:
                raise ValidationEvidenceError(
                    "verification evidence references an ExecutionResult that is not present"
                )
            if evidence.execution_result.action_id != evidence.execution_result_id:
                raise ValidationEvidenceError(
                    "verification evidence execution result id does not match the linked ExecutionResult"
                )

    @classmethod
    def from_snapshots(cls, before: WorkspaceSnapshot, after: WorkspaceSnapshot, verification_run) -> "ValidationEvidence":
        if before.repository != after.repository:
            raise ValidationEvidenceError("verification snapshots refer to different repositories")
        verification = tuple(
            VerificationEvidence.from_execution(execution)
            for execution in verification_run.executions
        )
        return cls(
            workspace=WorkspaceIdentity(
                repository=before.repository,
                revision_before=before.revision,
                revision_after=after.revision,
                fingerprint_before=before.fingerprint,
                fingerprint_after=after.fingerprint,
            ),
            # The after snapshot is the actual workspace validated against the Contract.
            change_context=after.change_context,
            execution_results=tuple(item.execution_result for item in verification),
            verification=verification,
        )

    @property
    def workspace_is_consistent(self) -> bool:
        return self.workspace.is_consistent

    @property
    def verification_summary(self):
        """A presentation projection retained for report compatibility only."""
        from code_governance.verification.runner import CommandResult, VerificationSummary

        return VerificationSummary(
            tuple(
                CommandResult(
                    command=" ".join(item.argv),
                    passed=item.passed,
                    returncode=item.actual_result.get("returncode"),
                    stdout=str(item.execution_result.metadata.get("stdout_preview", "")),
                    stderr=str(
                        item.execution_result.metadata.get(
                            "stderr_preview",
                            item.execution_result.summary if item.execution_failed else "",
                        )
                    ),
                    timed_out=(
                        item.execution_result.failure is not None
                        and item.execution_result.failure.category == "TOOL_TIMEOUT"
                    ),
                )
                for item in self.verification
            )
        )


def collect_workspace_snapshot(repo_path: Union[str, Path]) -> WorkspaceSnapshot:
    """Collect facts once, so validation can reject a workspace changed by verification."""
    repository = Path(repo_path).resolve()
    try:
        root = _run_git(repository, "rev-parse", "--show-toplevel").strip()
        revision = _run_git(repository, "rev-parse", "HEAD").strip()
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise GitContextError(f"unable to collect workspace identity: {detail.strip()}") from error
    context = collect_context(repository, revision=revision)
    return WorkspaceSnapshot(
        repository=str(Path(root).resolve()),
        revision=revision,
        fingerprint=_workspace_fingerprint(repository, revision),
        change_context=context,
    )


def _workspace_fingerprint(repository: Path, revision: str) -> str:
    """Hash all Git-visible worktree state without treating a report as fact."""
    digest = hashlib.sha256()
    digest.update(revision.encode("utf-8"))
    digest.update(b"\0tracked\0")
    digest.update(_run_git(repository, "diff", "--binary", revision).encode("utf-8"))
    digest.update(b"\0untracked\0")
    untracked = _run_git(repository, "ls-files", "--others", "--exclude-standard")
    for relative_path in sorted(path for path in untracked.splitlines() if path):
        path = repository / relative_path
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as error:
            raise GitContextError(f"unable to fingerprint untracked file: {path}") from error
        digest.update(b"\0")
    return digest.hexdigest()


def _run_git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
