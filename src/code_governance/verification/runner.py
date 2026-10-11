from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

from code_governance.contract.schema import VerificationClaim
from code_governance.runtime.execution import ExecutionResult


@dataclass(frozen=True)
class CommandResult:
    command: str
    passed: bool
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class VerificationSummary:
    commands: Tuple[CommandResult, ...]

    @property
    def all_passed(self) -> bool:
        return all(command.passed for command in self.commands)


class VerificationExecution:
    """The evidence linkage for one declared verification claim."""

    def __init__(self, claim: VerificationClaim, request, action, result: ExecutionResult):
        self.claim = claim
        self.request = request
        self.action = action
        self.result = result

    @property
    def claim_id(self) -> str:
        return self.claim.id

    @property
    def request_id(self) -> str:
        return self.request.id

    @property
    def action_id(self) -> str:
        return self.action.id

    @property
    def passed(self) -> bool:
        return self.result.success and self.result.metadata.get("returncode") == self.claim.expected_result.exit_code

    def command_result(self) -> CommandResult:
        return CommandResult(
            command=" ".join(self.request.argv),
            passed=self.passed,
            returncode=self.result.metadata.get("returncode"),
            stdout=str(self.result.metadata.get("stdout_preview", "")),
            stderr=str(self.result.metadata.get("stderr_preview", self.result.summary if not self.result.success else "")),
            timed_out=(
                self.result.failure is not None
                and self.result.failure.category == "TOOL_TIMEOUT"
            ),
        )


@dataclass(frozen=True)
class VerificationRun:
    executions: Tuple[VerificationExecution, ...]

    @property
    def summary(self) -> VerificationSummary:
        return VerificationSummary(tuple(execution.command_result() for execution in self.executions))


def execute_verification_claims(
    claims: Tuple[VerificationClaim, ...], executor, repo_path: Union[str, Path]
) -> VerificationRun:
    """Execute claims through the same canonical command Action path as an agent."""
    executions = []
    for claim in claims:
        request = claim.to_request()
        action = claim.to_action()
        result = executor.execute(action, Path(repo_path))
        executions.append(VerificationExecution(claim, request, action, result))
    return VerificationRun(tuple(executions))
