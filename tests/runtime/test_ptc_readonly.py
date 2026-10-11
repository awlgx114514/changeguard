import pytest
import json
import sys

from code_governance.agent.codex_agent import CodexAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.agent.contract_agent import ContractAgent
from code_governance.contract.schema import ChangeContract, Constraints, Expected, Intent, Metadata, Verification, VerificationClaim
from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.legacy_action_v1 import ActionType
from code_governance.runtime.action_validator import ActionValidationError, ActionValidator
from code_governance.runtime.loop import AgentLoop
from code_governance.runtime.state import AgentState
from code_governance.trace.writer import TraceManager
from code_governance.runtime.bounded_execution import BoundedToolExecutor
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.git import GitTool
from code_governance.tools.registry import ToolRegistry
from code_governance.tools.approval import ApprovalOutcome
from code_governance.tools.guards import HardGuard
from code_governance.tools.policy import PolicyDecision, PolicyResult
from code_governance.tools.policy import ToolPolicy, ToolRuntimeConfig


SAFE_PROGRAM = 'matches = tools.search_files("ExecutionResult")\nresult = {"matches": matches[:10]}'


class _ApprovedProvider:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def test_run_code_requires_explicit_ptc_mode():
    """A native-runtime regression must reject a PTC-only orchestration action."""
    action = Action.create(ActionType.RUN_CODE, input={"program": SAFE_PROGRAM})

    with pytest.raises(ActionValidationError, match="PTC"):
        ActionValidator().validate(action)

    ActionValidator(execution_mode="PTC").validate(action)


def test_ptc_decision_schema_and_prompt_expose_only_the_readonly_sdk():
    """PTC must be an explicit model capability, not a native-mode default."""
    native = CodexAgent()
    ptc = CodexAgent(execution_mode="PTC")

    assert "ptc.run" not in native.decision_schema["properties"]["operation"]["enum"]
    assert "ptc.run" in ptc.decision_schema["properties"]["operation"]["enum"]
    prompt = ptc._build_decision_prompt(type("State", (), {
        "task_description": "inspect", "task_id": "t", "current_goal": "inspect",
        "plan_reference": "", "context_reference": "", "recent_actions": (),
        "recent_observations": (), "errors": (), "status": "RUNNING",
    })(), {})
    assert "search_files, git_status, git_diff" not in prompt
    assert "write/edit" not in prompt


@pytest.mark.parametrize(
    "program",
    (
        "import os\nresult = {}",
        "result = open('secret.txt')",
        "result = subprocess.run([])",
        "result = tools.__class__",
        "callable_ref = tools.search_files\nresult = callable_ref('x')",
    ),
)
def test_restricted_program_rejects_capability_or_indirect_call_syntax(program):
    """Unsafe source must fail before the code worker could receive it."""
    from code_governance.runtime.ptc import PTCProgramRejected, validate_program

    with pytest.raises(PTCProgramRejected):
        validate_program(program)


def test_restricted_program_accepts_direct_safe_sdk_calls_and_bounded_control_flow():
    from code_governance.runtime.ptc import validate_program

    validate_program(
        "paths = tools.search_files('ExecutionResult')\n"
        "if len(paths) > 0:\n"
        "    first = paths[:10]\n"
        "else:\n"
        "    first = []\n"
        "for index in range(2):\n"
        "    first = first[:10]\n"
        "result = {'matches': first}"
    )


def test_restricted_worker_returns_a_bounded_curated_literal_result(tmp_path):
    """Removing the worker interpreter must break PTC execution, not just validation."""
    from code_governance.runtime.ptc import PTCCodeRuntime

    result = PTCCodeRuntime(timeout_seconds=2).execute(
        Action.create(ActionType.RUN_CODE, input={"program": "result = {'marker': 'PTC_MARKER'}"}),
        tmp_path,
    )

    assert result.status == "SUCCESS"
    assert result.metadata["curated_result"] == {"marker": "PTC_MARKER"}
    assert result.information_gain is False


def test_agent_loop_records_only_the_outer_ptc_observation(tmp_path):
    """Removing RUN_CODE dispatch must prevent its curated fact reaching the next decision."""
    from code_governance.runtime.ptc import PTCCodeRuntime

    class Agent:
        def __init__(self): self.contexts = []
        def decide(self, state, context):
            self.contexts.append(context)
            return (
                Action.create(ActionType.RUN_CODE, input={"program": "result = {'marker': 'NEXT_PTC_MARKER'}"})
                if len(self.contexts) == 1 else Action.create(ActionType.FINISH)
            )

    agent = Agent()
    state = AgentState.initialize("inspect", workspace=tmp_path)
    AgentLoop(agent, None, TraceManager("ptc-loop"), action_validator=ActionValidator("PTC"), ptc_runtime=PTCCodeRuntime()).run(state, {"base": "context"}, tmp_path)

    assert state.recent_observations[0].type == "ptc_result"
    assert "NEXT_PTC_MARKER" in state.recent_observations[0].summary


def test_ptc_worker_protocol_routes_requests_through_the_runtime(tmp_path):
    """A worker must receive a host response, never a host capability object."""
    script = (
        "import json,sys; json.loads(sys.stdin.readline()); "
        "print(json.dumps({'type':'tool_call','name':'git_status','args':[]})); sys.stdout.flush(); "
        "reply=json.loads(sys.stdin.readline()); print(json.dumps({'type':'result','result':reply['data']}))"
    )
    calls = []
    result = BoundedToolExecutor().run_ptc_worker(
        (sys.executable, "-c", script), tmp_path, timeout_seconds=2, action_id="ptc-parent",
        initial_request={"program": "result = {}"}, request_handler=lambda request: calls.append(request) or {"ok": True, "data": {"clean": True}},
    )

    assert result.status == "SUCCESS"
    assert json.loads(result.stdout)["result"] == {"clean": True}
    assert calls == [{"type": "tool_call", "name": "git_status", "args": []}]


def test_ptc_search_subcall_uses_the_canonical_registry_and_curates_result(tmp_path):
    """Replacing the registry bridge with a direct filesystem call must fail this contract."""
    from code_governance.runtime.ptc import PTCCodeRuntime
    (tmp_path / "needle.txt").write_text("x", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(FilesystemTool())

    result = PTCCodeRuntime(tool_registry=registry, timeout_seconds=2).execute(
        Action.create(ActionType.RUN_CODE, input={"program": "paths = tools.search_files('needle')\nresult = {'paths': paths}"}), tmp_path
    )

    assert result.status == "SUCCESS"
    assert result.metadata["curated_result"] == {"paths": ["needle.txt"]}
    assert result.metadata["subcall_count"] == 1


def test_ptc_subcall_policy_deny_is_retained_in_the_outer_result(tmp_path):
    from code_governance.runtime.ptc import PTCCodeRuntime
    registry = ToolRegistry(policy=ToolPolicy(ToolRuntimeConfig(allowed_tools=("git",))))
    registry.register(FilesystemTool())

    result = PTCCodeRuntime(tool_registry=registry, timeout_seconds=2).execute(
        Action.create(ActionType.RUN_CODE, input={"program": "paths = tools.search_files('needle')\nresult = {'paths': paths}"}), tmp_path
    )

    assert result.status == "FAILURE"
    assert result.failure.category == "PTC_SUBCALL_FAILURE"
    assert result.metadata["failed_subcalls"] == {"PTC_SUBCALL_POLICY_DENY": 1}


def test_ptc_worker_timeout_is_reported_only_after_quiescence(tmp_path):
    from code_governance.runtime.ptc import PTCCodeRuntime
    slow_worker = (sys.executable, "-c", "import time; time.sleep(30)")
    result = PTCCodeRuntime(timeout_seconds=0.1, worker_command=slow_worker).execute(
        Action.create(ActionType.RUN_CODE, input={"program": "result = {}"}), tmp_path
    )
    assert result.status == "TIMEOUT"
    assert result.failure.category == "PTC_CODE_TIMEOUT"


class _ScriptedAgent:
    def __init__(self, actions):
        self.actions = list(actions)
        self.contexts = []

    def decide(self, state, context):
        self.contexts.append(context)
        return self.actions.pop(0)


class _RecordingContextBuilder:
    def __init__(self):
        self.contexts = []

    def with_state(self, context, state):
        current = {
            "working_memory": tuple(item.summary for item in state.working_memory_summary),
            "active_issues": tuple(issue.description for issue in state.open_issues),
        }
        self.contexts.append(current)
        return current


def _contract():
    return ChangeContract(
        metadata=Metadata(project="ptc"),
        intent=Intent(goal="inspect"),
        expected=Expected(files=[], change_level="L1"),
        constraints=Constraints(forbidden=[]),
        verification=Verification(claims=[VerificationClaim(id="diff_check", description="Diff is valid.", request={"argv": ("git", "diff", "--check")})]),
    )


def _git_repo(path):
    import subprocess

    subprocess.run(("git", "init"), cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(("git", "config", "user.email", "test@example.com"), cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(("git", "config", "user.name", "Test User"), cwd=path, check=True, capture_output=True, text=True)
    (path / "needle.txt").write_text("needle\n", encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(("git", "commit", "-m", "initial"), cwd=path, check=True, capture_output=True, text=True)


def test_harness_ptc_mode_injects_validator_and_runtime(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    task_file = tmp_path / "task.md"
    task_file.write_text("Inspect the repository", encoding="utf-8")
    agent = _ScriptedAgent((
        Action.create(ActionType.RUN_CODE, input={"program": "result = {'marker': 'HARNESS_PTC'}"}),
        Action.create(ActionType.FINISH),
    ))

    harness = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda task, context: _contract()),
        coding_agent=agent,
        runtime_loop=True,
        execution_mode="PTC",
        approval_provider=_ApprovedProvider(),
    )

    result = harness.run(task_file, repo)

    assert result.success is True
    assert result.termination_reason == "MODEL_FINISH"


def test_ptc_trace_retains_full_child_result_and_parent_linkage(tmp_path):
    trace = TraceManager("ptc-trace")
    registry = ToolRegistry(trace_manager=trace)
    registry.register(FilesystemTool())
    (tmp_path / "needle.txt").write_text("x", encoding="utf-8")
    action = Action.create(
        ActionType.RUN_CODE,
        input={"program": "paths = tools.search_files('needle')\nresult = {'count': len(paths)}"},
    )

    result = __import__("code_governance.runtime.ptc", fromlist=["PTCCodeRuntime"]).PTCCodeRuntime(
        tool_registry=registry, timeout_seconds=2
    ).execute(action, tmp_path)

    assert result.success is True
    child_events = [event.data for event in trace.get_events() if event.type == "ptc_child_result"]
    assert len(child_events) == 1
    assert child_events[0]["parent_action_id"] == action.id
    assert child_events[0]["result"]["metadata"]["paths"] == ["needle.txt"]
    tool_events = [
        event.data for event in trace.get_events()
        if event.type in {"tool_called", "tool_finished"}
    ]
    assert tool_events and {event["parent_action_id"] for event in tool_events} == {action.id}


def test_ptc_outer_result_is_the_only_observation_context_fact(tmp_path):
    (tmp_path / "needle.txt").write_text("x", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(FilesystemTool())
    context_builder = _RecordingContextBuilder()
    agent = _ScriptedAgent((
        Action.create(
            ActionType.RUN_CODE,
            input={
                "program": (
                    "matches = tools.search_files('needle')\n"
                    "for index in range(9):\n"
                    "    matches = tools.search_files('needle')\n"
                    "result = {'marker': 'CURATED_ONLY', 'count': len(matches)}"
                )
            },
        ),
        Action.create(ActionType.FINISH),
    ))
    state = AgentState.initialize("inspect", workspace=tmp_path)

    AgentLoop(
        agent,
        registry,
        TraceManager("ptc-context"),
        context_builder=context_builder,
        action_validator=ActionValidator("PTC"),
        ptc_runtime=__import__("code_governance.runtime.ptc", fromlist=["PTCCodeRuntime"]).PTCCodeRuntime(
            tool_registry=registry, timeout_seconds=5
        ),
    ).run(state, {}, tmp_path)

    observation = state.recent_observations[0]
    assert observation.metadata["curated_result"] == {"marker": "CURATED_ONLY", "count": 1}
    assert "paths" not in observation.metadata
    assert "CURATED_ONLY" in context_builder.contexts[1]["working_memory"][0]
    assert "needle.txt" not in context_builder.contexts[1]["working_memory"][0]


def test_ptc_progress_uses_child_information_gain_not_worker_success(tmp_path):
    registry = ToolRegistry()
    registry.register(FilesystemTool())
    agent = _ScriptedAgent((
        Action.create(ActionType.RUN_CODE, input={"program": "result = {'marker': 'NO_CHILD'}"}),
        Action.create(ActionType.FINISH),
    ))
    trace = TraceManager("ptc-progress")
    state = AgentState.initialize("inspect", workspace=tmp_path)

    AgentLoop(
        agent,
        registry,
        trace,
        action_validator=ActionValidator("PTC"),
        ptc_runtime=__import__("code_governance.runtime.ptc", fromlist=["PTCCodeRuntime"]).PTCCodeRuntime(
            tool_registry=registry, timeout_seconds=2
        ),
    ).run(state, {}, tmp_path)

    progress = [event.data for event in trace.get_events() if event.type == "progress_evaluated"]
    assert progress[0] == {
        "action_id": state.recent_actions[0].id,
        "outcome": "NO_PROGRESS",
        "evidence": "no_state_change_or_information_gain",
    }


class _AskPolicy:
    def decide(self, action, tool_metadata, workspace):
        return PolicyResult(
            PolicyDecision.ASK,
            tool_name=tool_metadata["name"],
            canonical_target=str(action.operation.fields.get("path", action.operation.fields.get("name_query", ""))),
            category="READ",
        )


@pytest.mark.parametrize(
    "outcome,category",
    (
        (ApprovalOutcome.APPROVED, None),
        (ApprovalOutcome.REJECTED, "PTC_SUBCALL_APPROVAL_REJECTED"),
        (ApprovalOutcome.UNAVAILABLE, "PTC_SUBCALL_APPROVAL_UNAVAILABLE"),
    ),
)
def test_ptc_subcall_reenters_approval_governance(tmp_path, outcome, category):
    from code_governance.runtime.ptc import PTCCodeRuntime

    (tmp_path / "needle.txt").write_text("x", encoding="utf-8")
    trace = TraceManager("ptc-approval")
    provider = type("Provider", (), {"request": lambda self, request: outcome})()
    registry = ToolRegistry(
        trace_manager=trace,
        policy=_AskPolicy(),
        approval_provider=provider,
    )
    registry.register(FilesystemTool())
    action = Action.create(
        ActionType.RUN_CODE,
        input={"program": "paths = tools.search_files('needle')\nresult = {'marker': 'approval'}"},
    )

    result = PTCCodeRuntime(tool_registry=registry, timeout_seconds=2).execute(action, tmp_path)

    assert result.success is (category is None)
    if category is None:
        assert result.metadata["subcall_count"] == 1
        assert result.metadata["failed_subcalls"] == {}
        assert any(event.type == "tool_called" for event in trace.get_events())
    else:
        assert result.metadata["failed_subcalls"] == {category: 1}
        assert not any(event.type == "tool_called" for event in trace.get_events())
    assert [event.type for event in trace.get_events()].count("approval_requested") == 1
    assert [event.type for event in trace.get_events()].count("approval_resolved") == 1
    assert all(event.data["parent_action_id"] == action.id for event in trace.get_events() if event.type.startswith("approval_"))


def test_ptc_subcall_hard_guard_deny_is_retained(tmp_path):
    from code_governance.runtime.ptc import PTCCodeRuntime

    trace = TraceManager("ptc-guard")
    registry = ToolRegistry(
        trace_manager=trace,
        hard_guards=(HardGuard(prohibited_capabilities=("filesystem",)),),
    )
    registry.register(FilesystemTool())
    action = Action.create(
        ActionType.RUN_CODE,
        input={"program": "paths = tools.search_files('needle')\nresult = {'marker': 'guard'}"},
    )

    result = PTCCodeRuntime(tool_registry=registry, timeout_seconds=2).execute(action, tmp_path)

    assert result.metadata["failed_subcalls"] == {"PTC_SUBCALL_HARD_GUARD_DENY": 1}
    guard_event = next(event.data for event in trace.get_events() if event.type == "hard_guard_evaluated")
    assert guard_event["parent_action_id"] == action.id


def test_deterministic_two_tool_ptc_closed_loop_smoke(tmp_path):
    from code_governance.runtime.ptc import PTCCodeRuntime

    (tmp_path / "needle.txt").write_text("x", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(FilesystemTool())
    registry.register(GitTool())
    context_builder = _RecordingContextBuilder()
    agent = _ScriptedAgent((
        Action.create(
            ActionType.RUN_CODE,
            input={
                "program": (
                    "matches = tools.search_files('needle')\n"
                    "status = tools.git_status()\n"
                    "result = {'marker': 'TWO_TOOL_SMOKE', 'matches': len(matches), 'status': len(status['stdout'])}"
                )
            },
        ),
        Action.create(ActionType.FINISH),
    ))
    trace = TraceManager("ptc-smoke")
    registry.set_trace_manager(trace)
    state = AgentState.initialize("inspect", workspace=tmp_path)

    result = AgentLoop(
        agent,
        registry,
        trace,
        context_builder=context_builder,
        action_validator=ActionValidator("PTC"),
        ptc_runtime=PTCCodeRuntime(tool_registry=registry, timeout_seconds=5),
    ).run(state, {}, tmp_path)

    assert result.status == "MODEL_FINISHED"
    assert "TWO_TOOL_SMOKE" in context_builder.contexts[1]["working_memory"][0]
    assert result.recent_observations[0].metadata["subcall_count"] == 2
    assert len([event for event in trace.get_events() if event.type == "ptc_child_result"]) == 2
