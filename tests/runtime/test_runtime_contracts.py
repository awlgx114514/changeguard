from pathlib import Path
import json
import subprocess
import sys

from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.contract.schema import ChangeContract, Constraints, Expected, Intent, Metadata, Verification, VerificationClaim
from code_governance.runtime.action import Action, ActionType, OperationKind
from code_governance.runtime.execution import ExecutionResult
from code_governance.runtime.loop import AgentLoop
from code_governance.runtime.state import AgentState
from code_governance.runtime.target import TargetResolutionError, TargetResolver
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.registry import ToolRegistry
from code_governance.tools.shell import ShellTool
from code_governance.tools.approval import ApprovalOutcome
from code_governance.trace.writer import TraceManager
from code_governance.workspace.manager import WorkspaceError, WorkspaceManager


class ScriptedAgent:
    def __init__(self, actions):
        self.actions = list(actions)
        self.contexts = []

    def decide(self, state, context):
        self.contexts.append(context)
        return self.actions.pop(0)


class ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


class RecordingContextBuilder:
    def with_state(self, context, state):
        return {
            "base": context,
            "working_memory": tuple(item.summary for item in state.working_memory_summary),
        }


def _loop(agent, workspace, *, context_builder=None):
    trace = TraceManager("runtime-contracts")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    registry.register(ShellTool())
    state = AgentState.initialize("exercise runtime contracts", workspace=workspace)
    result = AgentLoop(
        agent,
        registry,
        trace,
        context_builder=context_builder,
    ).run(state, {"version": "v1"}, workspace)
    return result, trace


def test_model_schema_matches_all_runtime_operation_kinds():
    schema_values = set(Action.model_schema()["properties"]["operation"]["enum"])

    assert schema_values == {item.value for item in OperationKind}


def test_target_resolver_canonicalizes_equivalent_workspace_targets(tmp_path):
    target = tmp_path / "nested" / "note.txt"
    target.parent.mkdir()
    target.write_text("note", encoding="utf-8")
    resolver = TargetResolver()

    canonical = {
        resolver.resolve(Action.create_operation(OperationKind.FILE_READ, path=value), tmp_path).operation.fields["path"]
        for value in ("nested/note.txt", "./nested/note.txt", str(target))
    }

    assert canonical == {"nested/note.txt"}


def test_target_resolver_rejects_workspace_escape(tmp_path):
    with_target = Action.create_operation(OperationKind.FILE_READ, path="../outside.txt")

    try:
        TargetResolver().resolve(with_target, tmp_path)
    except TargetResolutionError as error:
        assert error.stage == "TARGET_RESOLUTION"
    else:
        raise AssertionError("workspace escape must be rejected")


def test_read_semantics_survive_execution_observation_history_and_context(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("decision-critical text", encoding="utf-8")
    agent = ScriptedAgent(
        (
            Action.create_operation(OperationKind.FILE_READ, path="notes.txt", reason="inspect source"),
            Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="ready to validate"),
        )
    )

    result, _ = _loop(agent, tmp_path, context_builder=RecordingContextBuilder())

    observation = result.recent_observations[0]
    assert result.recent_actions[0].operation.fields["path"] == "notes.txt"
    assert observation.artifact_reference == "notes.txt"
    assert observation.metadata["content_fingerprint"]
    assert "decision-critical text" in observation.metadata["content_preview"]
    assert "content_fingerprint=" in agent.contexts[1]["working_memory"][0]
    assert "decision-critical text" in agent.contexts[1]["working_memory"][0]


def test_search_read_paths_reach_the_next_decision_context(tmp_path):
    implementation = tmp_path / "src" / "solidity_analyzer" / "analyzer.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("def analyze(): pass\n", encoding="utf-8")
    tests = tmp_path / "tests" / "test_analyzer.py"
    tests.parent.mkdir()
    tests.write_text("def test_analyze(): pass\n", encoding="utf-8")
    agent = ScriptedAgent(
        (
            Action.create(
                ActionType.READ,
                "analyzer",
                input={"action": "search_file"},
                reason="locate implementation",
            ),
            Action.create(ActionType.FINISH, reason="search result available"),
        )
    )

    _loop(agent, tmp_path, context_builder=RecordingContextBuilder())

    next_context = agent.contexts[1]["working_memory"][0]
    assert "src/solidity_analyzer/analyzer.py" in next_context
    assert "tests/test_analyzer.py" in next_context


def test_read_context_retains_a_small_source_file_without_truncation(tmp_path):
    target = tmp_path / "filesystem.py"
    target.write_text("x" * 4200 + "\nWORKER_LAUNCH_MARKER\n", encoding="utf-8")
    agent = ScriptedAgent(
        (
            Action.create(ActionType.READ, "filesystem.py", reason="inspect worker launch"),
            Action.create(ActionType.FINISH),
        )
    )

    _loop(agent, tmp_path, context_builder=RecordingContextBuilder())

    assert "WORKER_LAUNCH_MARKER" in agent.contexts[1]["working_memory"][0]


def test_failed_command_output_reaches_next_decision_context(tmp_path):
    agent = ScriptedAgent(
        (
            Action.create(
                ActionType.RUN_COMMAND,
                input={
                    "command": (
                        sys.executable,
                        "-c",
                        "import sys; print('ASSERTION_MARKER'); sys.exit(1)",
                    )
                },
                reason="run focused regression test",
            ),
            Action.create(ActionType.FINISH, reason="failure was observed"),
        )
    )

    result, _ = _loop(agent, tmp_path, context_builder=RecordingContextBuilder())

    assert result.status == "MODEL_FINISHED"
    assert result.recent_observations[0].metadata["returncode"] == 1
    assert "ASSERTION_MARKER" in agent.contexts[1]["working_memory"][0]


def test_write_execution_result_reports_effect_and_state_change(tmp_path):
    registry = ToolRegistry()
    registry.register(FilesystemTool())
    action = Action.create_operation(
        OperationKind.FILE_CREATE,
        path="notes.txt",
        content="updated",
    )

    result = registry.execute(action, tmp_path)

    assert isinstance(result, ExecutionResult)
    assert result.status == "SUCCESS"
    assert result.canonical_target == "notes.txt"
    assert result.effect_type == "WRITE"
    assert result.state_changed is True
    assert result.result_fingerprint


def test_outside_workspace_path_fails_at_action_validation_boundary(tmp_path):
    agent = ScriptedAgent((
        Action.create(ActionType.READ, "../outside.txt"),
        Action.create(ActionType.FINISH),
    ))

    result, trace = _loop(agent, tmp_path)

    assert result.status == "MODEL_FINISHED"
    assert result.recent_observations[0].metadata["failure_stage"] == "ACTION_VALIDATION"
    observed = next(event.data for event in trace.get_events() if event.type == "observation_received")
    assert observed["failure_stage"] == "ACTION_VALIDATION"


def test_repeated_read_without_new_evidence_is_observed_and_next_action_can_progress(tmp_path):
    (tmp_path / "notes.txt").write_text("unchanged", encoding="utf-8")
    (tmp_path / "next.txt").write_text("new evidence", encoding="utf-8")
    agent = ScriptedAgent(
        (
            Action.create(ActionType.READ, "notes.txt"),
            Action.create(ActionType.READ, "./notes.txt"),
            Action.create(ActionType.READ, "next.txt"),
            Action.create(ActionType.FINISH),
        )
    )

    result, _ = _loop(agent, tmp_path, context_builder=RecordingContextBuilder())

    assert result.status == "MODEL_FINISHED"
    assert result.failure is None
    assert result.recent_observations[1].error_category == "REPEATED_READ_WITHOUT_NEW_EVIDENCE"
    assert result.recent_observations[2].success is True
    assert "new evidence" in result.recent_observations[2].metadata["content_preview"]
    recovery_context = "\n".join(agent.contexts[2]["working_memory"])
    assert "a successful read requires a different action" in recovery_context
    assert "notes.txt" in recovery_context


def test_repeated_read_after_new_command_evidence_can_reach_model_finish(tmp_path):
    (tmp_path / "notes.txt").write_text("unchanged", encoding="utf-8")
    agent = ScriptedAgent(
        (
            Action.create(ActionType.READ, "notes.txt"),
            Action.create(
                ActionType.RUN_COMMAND,
                input={"command": (sys.executable, "-c", "print('new evidence')")},
            ),
            Action.create(ActionType.READ, "notes.txt"),
            Action.create(ActionType.FINISH),
        )
    )

    result, _ = _loop(agent, tmp_path)

    assert result.status == "MODEL_FINISHED"
    assert result.failure is None


def test_legacy_write_without_v2_provenance_fails_at_action_validation(tmp_path):
    (tmp_path / "notes.txt").write_text("unchanged", encoding="utf-8")
    agent = ScriptedAgent(
        (
            Action.create(ActionType.READ, "notes.txt"),
            Action.create(ActionType.WRITE, "notes.txt", input={"content": "unchanged"}),
            Action.create(ActionType.FINISH),
        )
    )

    result, trace = _loop(agent, tmp_path)

    assert result.status == "MODEL_FINISHED"
    progress = [event.data for event in trace.get_events() if event.type == "progress_evaluated"]
    assert progress[1]["outcome"] == "FAILURE"
    assert result.recent_observations[1].error_category == "INVALID_ACTION"


def _git_repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    for command in (
        ("init",),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test User"),
        ("add", "."),
        ("commit", "-m", "initial"),
    ):
        subprocess.run(("git", *command), cwd=repository, check=True, capture_output=True, text=True)
    return repository


def _contract(*, expected=(), forbidden=()):
    return ChangeContract(
        metadata=Metadata(project="runtime-contracts"),
        intent=Intent(goal="exercise validation"),
        expected=Expected(files=list(expected), change_level="L1"),
        constraints=Constraints(forbidden=list(forbidden)),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )


def test_finish_transitions_to_verification_then_validator_success(tmp_path):
    repository = _git_repository(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Do not change the workspace", encoding="utf-8")
    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: _contract()),
        coding_agent=ScriptedAgent((Action.create(ActionType.FINISH),)),
        runtime_loop=True,
        approval_provider=ApprovedProvider(),
    )

    result = harness.run(task_file, repository)
    trace = json.loads(next((repository / ".agent" / "runs").glob("*/trace.json")).read_text(encoding="utf-8"))
    states = [event["data"]["status"] for event in trace["events"] if event["type"] == "state_updated"]

    assert result.success is True
    assert states[-3:] == ["MODEL_FINISHED", "VERIFYING", "SUCCEEDED"]


def test_repeated_read_observation_allows_model_finish_and_validation(tmp_path):
    repository = _git_repository(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Do not change the workspace", encoding="utf-8")
    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: _contract()),
        coding_agent=ScriptedAgent((
            Action.create(ActionType.READ, "app.py"),
            Action.create(ActionType.READ, "./app.py"),
            Action.create(ActionType.FINISH),
        )),
        runtime_loop=True,
        approval_provider=ApprovedProvider(),
    )

    result = harness.run(task_file, repository)
    trace = json.loads(next((repository / ".agent" / "runs").glob("*/trace.json")).read_text(encoding="utf-8"))
    states = [event["data"]["status"] for event in trace["events"] if event["type"] == "state_updated"]

    assert result.success is True
    assert result.termination_reason == "MODEL_FINISH"
    assert result.validation_result == "PASS"
    assert states[-2:] == ["VERIFYING", "SUCCEEDED"]


def test_repeated_read_observation_cannot_mask_workspace_failure(tmp_path):
    class PatchFailureWorkspaceManager(WorkspaceManager):
        def save_patch(self, workspace):
            raise WorkspaceError("workspace is no longer available")

    repository = _git_repository(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Do not change the workspace", encoding="utf-8")
    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: _contract()),
        coding_agent=ScriptedAgent((
            Action.create(ActionType.READ, "app.py"),
            Action.create(ActionType.READ, "./app.py"),
            Action.create(ActionType.FINISH),
        )),
        workspace_manager=PatchFailureWorkspaceManager(),
        runtime_loop=True,
        approval_provider=ApprovedProvider(),
    )

    result = harness.run(task_file, repository)

    assert result.success is False
    assert result.termination_reason == "MODEL_FINISH"
    assert result.validation_result == "PASS"
    assert result.final_outcome == "INFRASTRUCTURE_FAILED"


def test_validator_failure_is_not_a_successful_finish(tmp_path):
    repository = _git_repository(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Do not change app.py", encoding="utf-8")
    tool_registry = ToolRegistry()
    tool_registry.register(FilesystemTool())
    tool_registry.register(ShellTool())

    class ReplaceAgent:
        def __init__(self):
            self.calls = 0

        def decide(self, state, context):
            self.calls += 1
            if self.calls == 1:
                return Action.create_operation(OperationKind.FILE_READ, path="app.py")
            if self.calls == 2:
                return Action.create_operation(
                    OperationKind.FILE_REPLACE,
                    path="app.py",
                    expected_text="VALUE = 1",
                    replacement="VALUE = 2",
                    observed_file_hash=state.recent_observations[-1].metadata["file_hash"],
                )
            return Action.create_operation(OperationKind.WORKFLOW_FINISH)

    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: _contract(forbidden=("app.py",))),
        coding_agent=ReplaceAgent(),
        tool_registry=tool_registry,
        runtime_loop=True,
        max_repairs=0,
    )

    result = harness.run(task_file, repository)

    assert result.success is False
    assert result.report.status == "FAIL"
    assert result.failure.stage == "VALIDATION"
