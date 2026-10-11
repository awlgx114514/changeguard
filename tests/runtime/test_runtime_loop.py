from pathlib import Path
import subprocess
import sys

import hashlib

from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.action_executor import CanonicalActionExecutor
from code_governance.runtime.loop import AgentLoop
from code_governance.runtime.observation import Observation
from code_governance.runtime.state import AgentState
from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.contract.schema import (
    ChangeContract,
    Constraints,
    Expected,
    Intent,
    Metadata,
    Verification,
    VerificationClaim,
)
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.shell import ShellTool
from code_governance.tools.approval import ApprovalOutcome
from code_governance.tools.policy import ToolPolicy
from code_governance.tools.registry import ToolRegistry
from code_governance.trace.writer import TraceManager


def run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def prepare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    package = repo / "src" / "demo"
    package.mkdir(parents=True)
    run_git(repo, "init")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test User")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text(
        "from demo import helper\n",
        encoding="utf-8",
    )
    (package / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "initial")
    return repo


def test_action_serializes_with_its_input_and_reason():
    action = Action.create_operation(OperationKind.FILE_CREATE, path="notes.txt", content="runtime", reason="record the task result")

    restored = Action.from_dict(action.to_dict())

    assert restored == action
    assert restored.to_dict()["operation"] == "file.create"


def test_state_saves_and_restores_actions_and_observations(tmp_path):
    state = AgentState.initialize("update notes", {"version": "v1"}, {"steps": []})
    action = Action.create_operation(OperationKind.FILE_READ, path="notes.txt", reason="inspect")
    observation = Observation(
        action.id,
        type="file_content",
        success=True,
        metadata={"content_length": len("old")},
    )
    state.record_action(action)
    state.record_observation(observation)
    path = tmp_path / "state.json"

    state.save(path)
    restored = AgentState.load(path)

    assert restored.to_dict() == state.to_dict()
    assert restored.status == "CREATED"
    assert restored.max_steps == 10


class DecidingAgent:
    def __init__(self, actions):
        self._actions = list(actions)

    def decide(self, state, context):
        return self._actions.pop(0)


class RejectedApprovalProvider:
    def request(self, request):
        return ApprovalOutcome.REJECTED


class ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


class RecordingHistoryManager:
    def __init__(self):
        self.records = []

    def record(self, state, action, observation):
        self.records.append((state, action, observation))
        return {"working_items": 0, "active_issues": 0}


class RecordingObserver:
    def __init__(self):
        self.contexts = []
        self.states = []

    def on_context(self, context):
        self.contexts.append(context)

    def on_runtime_state(self, state):
        self.states.append(state)


class RecordingActionValidator:
    def __init__(self):
        self.actions = []

    def validate(self, action):
        self.actions.append(action)


class RecordingCanonicalExecutor(CanonicalActionExecutor):
    def __init__(self, registry):
        super().__init__(registry)
        self.prepared = []
        self.executed = []

    def prepare(self, action, workspace):
        self.prepared.append(action)
        return super().prepare(action, workspace)

    def execute_prepared(self, action, workspace):
        self.executed.append(action)
        return super().execute_prepared(action, workspace)


def test_loop_executes_tool_actions_emits_trace_and_finishes(tmp_path):
    trace = TraceManager("run-1")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    agent = DecidingAgent(
        (
            Action.create_operation(OperationKind.FILE_CREATE, path="notes.txt", content="runtime", reason="write requested result"),
            Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="task complete"),
        )
    )
    state = AgentState.initialize("write notes", {"version": "v1"}, {"steps": []})

    result = AgentLoop(agent, registry, trace).run(state, {"version": "v1"}, tmp_path)

    assert result.status == "MODEL_FINISHED"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "runtime"
    assert [item.success for item in result.recent_observations] == [True, True]
    event_types = [event.type for event in trace.get_events()]
    assert {
        "action_decided",
        "action_executed",
        "observation_received",
        "state_updated",
        "history_compressed",
    } <= set(event_types)
    assert "tool_called" in event_types
    assert "tool_finished" in event_types
    tool_event = next(event.data for event in trace.get_events() if event.type == "tool_finished")
    assert tool_event["action_id"] == result.recent_actions[0].id
    assert tool_event["tool_name"] == "filesystem"
    assert tool_event["duration"] >= 0
    assert event_types[-1] == "agent_loop_finished"
    state_events = [event.data for event in trace.get_events() if event.type == "state_updated"]
    assert state_events[-1]["status"] == "MODEL_FINISHED"


def test_loop_delegates_tool_actions_to_the_canonical_action_executor(tmp_path):
    trace = TraceManager("canonical-executor-loop")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    executor = RecordingCanonicalExecutor(registry)
    agent = DecidingAgent((
        Action.create_operation(OperationKind.FILE_CREATE, path="notes.txt", content="runtime"),
        Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete"),
    ))

    result = AgentLoop(agent, registry, trace, action_executor=executor).run(
        AgentState.initialize("write notes", {}, {}), {}, tmp_path
    )

    assert result.status == "MODEL_FINISHED"
    assert [action.operation.kind for action in executor.prepared] == [
        OperationKind.FILE_CREATE,
        OperationKind.WORKFLOW_FINISH,
    ]
    assert [action.operation.kind for action in executor.executed] == [OperationKind.FILE_CREATE]
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "runtime"


def test_loop_records_approval_rejection_as_pre_execution_observation(tmp_path):
    trace = TraceManager("approval-rejection")
    registry = ToolRegistry(
        trace_manager=trace,
        policy=ToolPolicy(),
        approval_provider=RejectedApprovalProvider(),
    )
    registry.register(FilesystemTool())
    agent = DecidingAgent((
        Action.create_operation(OperationKind.FILE_CREATE, path="notes.txt", content="blocked"),
        Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete"),
    ))
    state = AgentState.initialize("write notes", {}, {})

    result = AgentLoop(agent, registry, trace).run(state, {}, tmp_path)

    observation = result.recent_observations[0]
    assert observation.success is False
    assert observation.metadata["failure_stage"] == "PRE_EXECUTION_GOVERNANCE"
    assert observation.error_category == "APPROVAL_REJECTED"
    assert not (tmp_path / "notes.txt").exists()


def test_write_action_trace_metadata_is_compact_and_privacy_safe(tmp_path):
    trace = TraceManager("run-1")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    agent = DecidingAgent(
        (
            Action.create_operation(OperationKind.FILE_CREATE, path="notes.txt", content="private source text", reason="write private content"),
            Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete"),
        )
    )
    state = AgentState.initialize("write notes", {}, {})

    AgentLoop(agent, registry, trace).run(state, {}, tmp_path)

    event = next(event.data for event in trace.get_events() if event.type == "action_executed")
    assert set(event) == {"action_id", "operation", "target", "tool", "success"}
    assert event["operation"] == "file.create"
    assert event["target"] == "notes.txt"
    assert event["tool"] == "filesystem"
    assert event["success"] is True
    assert isinstance(event["action_id"], str) and event["action_id"]
    assert "input" not in event
    assert "private source text" not in str(event)


def test_loop_stops_when_finish_action_is_selected(tmp_path):
    trace = TraceManager("run-1")
    registry = ToolRegistry(trace_manager=trace)
    agent = DecidingAgent((Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="done"),))
    state = AgentState.initialize("finish", {}, {})

    result = AgentLoop(agent, registry, trace).run(state, {}, tmp_path)

    assert result.status == "MODEL_FINISHED"
    assert len(result.recent_actions) == 1


def test_loop_stops_after_consecutive_action_failures(tmp_path):
    trace = TraceManager("run-1")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    agent = DecidingAgent(
        (
            Action.create_operation(OperationKind.FILE_READ, path="missing.txt", reason="inspect"),
            Action.create_operation(OperationKind.FILE_READ, path="missing.txt", reason="retry"),
        )
    )
    state = AgentState.initialize("inspect", {}, {})

    result = AgentLoop(agent, registry, trace, max_consecutive_failures=2).run(state, {}, tmp_path)

    assert result.status == "FAILED"
    assert len(result.recent_actions) == 2
    assert "consecutive action failures" in result.errors[-1]
    state_events = [event.data for event in trace.get_events() if event.type == "state_updated"]
    assert state_events[-1]["status"] == "FAILED"


def test_loop_returns_an_invalid_action_to_the_next_decision(tmp_path):
    trace = TraceManager("invalid-action-recovery")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(ShellTool())
    agent = DecidingAgent((
        Action.create_operation(OperationKind.COMMAND_RUN, reason="missing command"),
        Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="stop after rejection"),
    ))

    result = AgentLoop(agent, registry, trace).run(
        AgentState.initialize("recover invalid action", {}, {}),
        {},
        tmp_path,
    )

    assert result.status == "MODEL_FINISHED"
    assert result.recent_observations[0].error_category == "INVALID_ACTION"
    assert result.recent_observations[0].success is False
    assert len(result.recent_actions) == 2


def test_loop_stops_as_no_progress_after_verification_and_invalid_action(tmp_path):
    trace = TraceManager("verified-invalid-action")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(ShellTool())
    agent = DecidingAgent((
        Action.create_operation(OperationKind.COMMAND_RUN, argv=[sys.executable, "-c", "pass"]),
        Action.create_operation(OperationKind.COMMAND_RUN, reason="missing command"),
    ))

    result = AgentLoop(agent, registry, trace).run(
        AgentState.initialize("stop after verified rejection", {}, {}),
        {},
        tmp_path,
    )

    assert result.status == "STOPPED"
    assert result.termination_reason == "NO_PROGRESS"
    assert result.recent_observations[-1].error_category == "INVALID_ACTION"


def test_loop_blocks_repeated_write_without_new_evidence(tmp_path):
    (tmp_path / "notes.txt").write_text("base\n", encoding="utf-8")
    trace = TraceManager("write-safety-duplicate")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    base_hash = hashlib.sha256(b"base\n").hexdigest()
    agent = DecidingAgent((
        Action.create_operation(OperationKind.FILE_READ, path="notes.txt"),
        Action.create_operation(OperationKind.FILE_WRITE, path="notes.txt", content="first\n", observed_file_hash=base_hash),
        Action.create_operation(OperationKind.FILE_WRITE, path="notes.txt", content="second\n", observed_file_hash=hashlib.sha256(b"first\n").hexdigest()),
        Action.create_operation(OperationKind.WORKFLOW_FINISH),
    ))

    result = AgentLoop(agent, registry, trace).run(
        AgentState.initialize("update notes", {}, {}),
        {},
        tmp_path,
    )

    assert result.status == "MODEL_FINISHED"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "first\n"
    assert result.recent_observations[2].error_category == "REPEATED_WRITE_WITHOUT_NEW_EVIDENCE"


def test_loop_allows_repair_write_after_new_verification_evidence(tmp_path):
    (tmp_path / "notes.txt").write_text("base\n", encoding="utf-8")
    trace = TraceManager("write-safety-verification")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    registry.register(ShellTool())
    base_hash = hashlib.sha256(b"base\n").hexdigest()
    agent = DecidingAgent((
        Action.create_operation(OperationKind.FILE_READ, path="notes.txt"),
        Action.create_operation(OperationKind.FILE_WRITE, path="notes.txt", content="first\n", observed_file_hash=base_hash),
        Action.create_operation(OperationKind.COMMAND_RUN, argv=[sys.executable, "-c", "print('verified')"]),
        Action.create_operation(OperationKind.FILE_READ, path="notes.txt"),
        Action.create_operation(OperationKind.FILE_WRITE, path="notes.txt", content="repaired\n", observed_file_hash=hashlib.sha256(b"first\n").hexdigest()),
        Action.create_operation(OperationKind.WORKFLOW_FINISH),
    ))

    result = AgentLoop(agent, registry, trace).run(
        AgentState.initialize("update notes", {}, {}),
        {},
        tmp_path,
    )

    assert result.status == "MODEL_FINISHED"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "repaired\n"
    assert all(item.success for item in result.recent_observations)


def test_loop_blocks_full_file_overwrite_after_truncated_read_and_preserves_source(tmp_path):
    original = "header\n" + "x" * 9000 + "\nunchanged-tail\n"
    (tmp_path / "notes.txt").write_text(original, encoding="utf-8")
    trace = TraceManager("write-safety-truncated")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    agent = DecidingAgent((
        Action.create_operation(OperationKind.FILE_READ, path="notes.txt", line_count=1),
        Action.create_operation(OperationKind.FILE_WRITE, path="notes.txt", content="replacement\n", observed_file_hash=hashlib.sha256(original.encode()).hexdigest()),
        Action.create_operation(OperationKind.WORKFLOW_FINISH),
    ))

    result = AgentLoop(agent, registry, trace).run(
        AgentState.initialize("update notes", {}, {}),
        {},
        tmp_path,
    )

    assert result.status == "MODEL_FINISHED"
    assert result.recent_observations[0].metadata["content_truncated"] is True
    assert result.recent_observations[1].error_category == "FULL_FILE_WRITE_REQUIRES_COMPLETE_READ"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == original


def test_registry_executes_action_through_its_tool_gateway(tmp_path):
    trace = TraceManager("run-1")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    action = Action.create_operation(OperationKind.FILE_CREATE, path="gateway.txt", content="through registry", reason="exercise the action gateway")

    execution = registry.execute(action, tmp_path)

    assert execution.status == "SUCCESS"
    assert execution.effect_type == "WRITE"
    assert execution.metadata["written"] == len("through registry")


def test_harness_runtime_mode_runs_loop_then_validates(tmp_path):
    repo = prepare_repo(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Update src/demo/core.py", encoding="utf-8")
    agent = DecidingAgent(
        (
            Action.create_operation(OperationKind.FILE_WRITE, path="src/demo/core.py", content="VALUE = 2\n", observed_file_hash=hashlib.sha256((repo / "src/demo/core.py").read_bytes()).hexdigest(), reason="apply requested update"),
            Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete"),
        )
    )
    contract = ChangeContract(
        metadata=Metadata(project="demo"),
        intent=Intent(goal="update core"),
        expected=Expected(files=["src/demo/core.py"], change_level="L1"),
        constraints=Constraints(forbidden=[]),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )
    tool_registry = ToolRegistry()
    tool_registry.register(FilesystemTool())
    tool_registry.register(ShellTool())
    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: contract),
        coding_agent=agent,
        tool_registry=tool_registry,
        runtime_loop=True,
    )

    result = harness.run(task_file, repo)
    trace_path = next((repo / ".agent" / "runs").glob("*/trace.json"))
    trace = __import__("json").loads(trace_path.read_text(encoding="utf-8"))

    assert result.success is True
    assert [event["type"] for event in trace["events"]].count("agent_loop_finished") == 1


def test_harness_runtime_mode_injects_history_manager_factory(tmp_path):
    repo = prepare_repo(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Finish the task", encoding="utf-8")
    agent = DecidingAgent((Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete"),))
    contract = ChangeContract(
        metadata=Metadata(project="demo"),
        intent=Intent(goal="finish task"),
        expected=Expected(files=[], change_level="L1"),
        constraints=Constraints(forbidden=[]),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )
    managers = []

    def history_manager_factory():
        manager = RecordingHistoryManager()
        managers.append(manager)
        return manager

    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: contract),
        coding_agent=agent,
        runtime_loop=True,
        history_manager_factory=history_manager_factory,
        approval_provider=ApprovedProvider(),
    )

    result = harness.run(task_file, repo)

    assert result.success is True
    assert len(managers) == 1
    assert len(managers[0].records) == 1


def test_runtime_harness_observer_receives_read_only_evidence(tmp_path):
    repo = prepare_repo(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Finish the task", encoding="utf-8")
    observer = RecordingObserver()
    agent = DecidingAgent((Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete"),))
    contract = ChangeContract(
        metadata=Metadata(project="demo"),
        intent=Intent(goal="finish task"),
        expected=Expected(files=[], change_level="L1"),
        constraints=Constraints(forbidden=[]),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )
    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: contract),
        coding_agent=agent,
        runtime_loop=True,
        observer=observer,
        approval_provider=ApprovedProvider(),
    )

    result = harness.run(task_file, repo)

    assert result.success is True
    assert len(observer.contexts) == 1
    assert observer.contexts[0].version == "v1"
    assert observer.states[-1].status == "SUCCEEDED"
    assert not hasattr(observer.states[-1], "context")


def test_harness_runtime_mode_injects_action_validator_factory(tmp_path):
    repo = prepare_repo(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Finish the task", encoding="utf-8")
    validator = RecordingActionValidator()
    agent = DecidingAgent((Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete"),))
    contract = ChangeContract(
        metadata=Metadata(project="demo"),
        intent=Intent(goal="finish task"),
        expected=Expected(files=[], change_level="L1"),
        constraints=Constraints(forbidden=[]),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )
    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: contract),
        coding_agent=agent,
        runtime_loop=True,
        action_validator_factory=lambda: validator,
        approval_provider=ApprovedProvider(),
    )

    result = harness.run(task_file, repo)

    assert result.success is True
    assert [action.operation.kind for action in validator.actions] == [
        OperationKind.WORKFLOW_FINISH,
        OperationKind.COMMAND_RUN,
    ]


def test_harness_runtime_repair_false_never_uses_direct_provider_execution(tmp_path):
    class RepairTrackingAgent:
        def __init__(self):
            self.decide_calls = 0
            self.run_calls = []

        def decide(self, state, context):
            self.decide_calls += 1
            if self.decide_calls == 1:
                return Action.create_operation(OperationKind.FILE_CREATE, path="forbidden.py", content="changed\n", reason="write a forbidden file")
            return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="complete")

        def run(self, task, contract, plan, workspace, context):
            self.run_calls.append((task, plan))
            return CodingAgentResult(stdout="repair complete", stderr="", returncode=0)

    repo = prepare_repo(tmp_path)
    task_file = tmp_path / "task.md"
    task_file.write_text("Repair the task", encoding="utf-8")
    agent = RepairTrackingAgent()
    contract = ChangeContract(
        metadata=Metadata(project="demo"),
        intent=Intent(goal="repair forbidden file"),
        expected=Expected(files=[], change_level="L1"),
        constraints=Constraints(forbidden=["forbidden.py"]),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )
    tool_registry = ToolRegistry()
    tool_registry.register(FilesystemTool())
    tool_registry.register(ShellTool())
    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: contract),
        coding_agent=agent,
        tool_registry=tool_registry,
        runtime_loop=True,
        runtime_repair=False,
        max_repairs=1,
    )

    result = harness.run(task_file, repo)

    assert result.success is False
    assert result.report.status == "FAIL"
    assert agent.decide_calls == 2
    assert agent.run_calls == []
