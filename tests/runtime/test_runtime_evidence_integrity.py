import json
import sys
from pathlib import Path

from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.action_executor import CanonicalActionExecutor
from code_governance.runtime.execution import ExecutionResult, FailureRecord
from code_governance.runtime.observation import Observation
from code_governance.runtime.progress import ProgressEvaluator
from code_governance.runtime.state import AgentState
from code_governance.tools.approval import ApprovalOutcome
from code_governance.tools.policy import ToolPolicy, ToolRuntimeConfig
from code_governance.tools.registry import ToolRegistry
from code_governance.tools.shell import ShellTool


class _Approved:
    def request(self, request):
        return ApprovalOutcome.APPROVED


def _executor(timeout=0.2):
    registry = ToolRegistry(
        policy=ToolPolicy(ToolRuntimeConfig(shell_timeout_seconds=timeout)),
        approval_provider=_Approved(),
    )
    registry.register(ShellTool())
    return CanonicalActionExecutor(registry)


def _command(source):
    return Action.create_operation(
        OperationKind.COMMAND_RUN,
        argv=(sys.executable, "-c", source),
        reason="exercise bounded command execution",
    )


def test_t1_timeout_after_durable_write_is_effect_uncertain(tmp_path):
    target = tmp_path / "durable.txt"
    code = (
        "from pathlib import Path; import os, time; "
        f"p=Path({str(target)!r}); f=p.open('w'); f.write('committed'); "
        "f.flush(); os.fsync(f.fileno()); f.close(); time.sleep(3)"
    )

    result = _executor().execute(_command(code), tmp_path)

    assert target.read_text(encoding="utf-8") == "committed"
    assert result.effect_type == "COMMAND"
    assert result.metadata.get("effect_uncertain") is True
    assert result.status in {"TIMEOUT", "FAILURE"}


def test_t1b_invoked_write_exception_is_uncertain_even_when_file_changed(tmp_path):
    from code_governance.tools.filesystem import FilesystemTool

    class WriteThenRaise(FilesystemTool):
        def execute(self, payload, workspace):
            result = super().execute(payload, workspace)
            raise RuntimeError("failure after write returned from filesystem operation")

    registry = ToolRegistry()
    registry.register(WriteThenRaise())
    action = Action.create_operation(OperationKind.FILE_CREATE, path="created.txt", content="done")
    result = CanonicalActionExecutor(registry).execute(action, tmp_path)

    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "done"
    assert result.effect_type == "WRITE"
    assert result.state_changed is False
    assert result.metadata["effect_uncertain"] is True


def test_t2_timeout_stdout_survives_result_observation_and_history(tmp_path):
    from code_governance.runtime.history import HistoryManager

    state = AgentState.initialize("timeout evidence", workspace=tmp_path)
    action = _command("import time; print('STDOUT_EVIDENCE_MARKER', flush=True); time.sleep(3)")
    state.record_action(action)
    result = _executor().execute(action, tmp_path)
    observation = Observation.from_execution(result, "command_result")
    state.record_observation(observation)
    HistoryManager().record(state, action, observation)

    assert "STDOUT_EVIDENCE_MARKER" in result.metadata.get("stdout_preview", "")
    assert "STDOUT_EVIDENCE_MARKER" in observation.summary
    assert "STDOUT_EVIDENCE_MARKER" in state.working_memory_summary[0].summary


def test_t3_timeout_stderr_is_separate_and_not_crowded_out_by_stdout(tmp_path):
    source = (
        "import sys,time; print('O'*9000, flush=True); "
        "print('STDERR_EVIDENCE_MARKER', file=sys.stderr, flush=True); time.sleep(3)"
    )
    result = _executor().execute(_command(source), tmp_path)

    assert result.metadata.get("stdout_length", 0) >= 4096
    assert "STDERR_EVIDENCE_MARKER" in result.metadata.get("stderr_preview", "")
    assert "STDERR_EVIDENCE_MARKER" in result.summary


def test_t2_direct_timeoutexpired_is_authoritative_and_streams_are_preserved(tmp_path):
    import subprocess

    class DirectTimeout(ShellTool):
        def execute(self, payload, workspace):
            raise subprocess.TimeoutExpired(
                payload["command"], 0.1,
                output=b"DIRECT_TIMEOUT_STDOUT", stderr=b"DIRECT_TIMEOUT_STDERR",
            )

    registry = ToolRegistry()
    registry.register(DirectTimeout())
    result = CanonicalActionExecutor(registry).execute(_command("pass"), tmp_path)

    assert result.metadata["failure_category"] == "TOOL_TIMEOUT"
    assert result.metadata["stdout_preview"] == "DIRECT_TIMEOUT_STDOUT"
    assert result.metadata["stderr_preview"] == "DIRECT_TIMEOUT_STDERR"
    assert result.metadata["output_partial"] is True
    assert result.metadata["effect_uncertain"] is True


def test_t4_no_output_timeout_is_partial_without_phantom_output(tmp_path):
    result = _executor().execute(_command("import time; time.sleep(3)"), tmp_path)

    assert result.metadata.get("effect_uncertain") is True
    assert result.metadata.get("output_partial") is True
    assert result.metadata.get("stdout_preview") == ""
    assert result.metadata.get("stderr_preview") == ""
    assert "None" not in result.summary
    assert "timed out after" in result.failure.detail
    assert "timed out after" not in result.metadata["stderr_preview"]
    assert "returncode" not in result.metadata


def test_effect_qualifier_distinguishes_confirmed_partial_change():
    from code_governance.runtime.execution import evidence_qualifiers

    qualifier = evidence_qualifiers({"effect_uncertain": True, "state_changed": True})[0]

    assert "partial change is confirmed" in qualifier
    assert "no change is confirmed" not in qualifier


def test_t5_ptc_child_omission_reaches_parent_result(tmp_path):
    from code_governance.runtime.bounded_execution import BoundedExecution
    from code_governance.runtime.execution import ExecutionResult
    from code_governance.runtime.ptc import PTCCodeRuntime

    class ChildRegistry:
        _trace_manager = None

        def execute(self, action, workspace, parent_action_id=""):
            return ExecutionResult(
                action_id=action.id,
                status="SUCCESS",
                effect_type="READ",
                information_gain=True,
                metadata={"paths": [f"f{i}.py" for i in range(150)]},
            )

    class WorkerExecutor:
        def run_ptc_worker(self, *args, **kwargs):
            kwargs["request_handler"]({"name": "search_files", "args": ["f"]})
            return BoundedExecution(
                status="SUCCESS", returncode=0,
                stdout=json.dumps({"result": {"count": 150}}), stderr="", quiescent=True,
            )

    action = Action.create_operation(
        OperationKind.PTC_RUN,
        program="paths = tools.search_files('f')\nresult = {'count': len(paths)}",
    )
    result = PTCCodeRuntime(tool_registry=ChildRegistry(), executor=WorkerExecutor()).execute(action, tmp_path)

    assert result.success
    assert result.metadata["truncated"] is True


def test_t5_child_declared_omission_reaches_ptc_parent(tmp_path):
    from code_governance.runtime.bounded_execution import BoundedExecution
    from code_governance.runtime.execution import ExecutionResult
    from code_governance.runtime.ptc import PTCCodeRuntime

    class ChildRegistry:
        _trace_manager = None

        def execute(self, action, workspace, parent_action_id=""):
            return ExecutionResult(action_id=action.id, status="SUCCESS", metadata={"truncated": True})

    class WorkerExecutor:
        def run_ptc_worker(self, *args, **kwargs):
            kwargs["request_handler"]({"name": "search_files", "args": ["x"]})
            return BoundedExecution("SUCCESS", 0, json.dumps({"result": 1}), "", True)

    action = Action.create_operation(OperationKind.PTC_RUN, program="paths = tools.search_files('x')\nresult = 1")
    result = PTCCodeRuntime(tool_registry=ChildRegistry(), executor=WorkerExecutor()).execute(action, tmp_path)
    assert result.success and result.metadata["truncated"] is True


def test_t4_ptc_timeout_marks_empty_output_partial(tmp_path):
    from code_governance.runtime.bounded_execution import BoundedExecution
    from code_governance.runtime.ptc import PTCCodeRuntime

    class WorkerExecutor:
        def run_ptc_worker(self, *args, **kwargs):
            return BoundedExecution("TIMEOUT", None, "", "", True)

    action = Action.create_operation(OperationKind.PTC_RUN, program="result = 1")
    result = PTCCodeRuntime(executor=WorkerExecutor()).execute(action, tmp_path)

    assert result.metadata["output_partial"] is True
    assert result.metadata["effect_uncertain"] is False
    assert "Output partial:" in result.summary


def test_t6_ptc_nested_container_omissions_are_aggregated():
    from code_governance.runtime.ptc import _bounded_value

    value = {
        "long_value": "x" * 5000,
        "nested": {"items": ["y" * 5000]},
        "long_key_" + ("k" * 300): "value",
        "many": list(range(101)),
        "deep": [[[[[[["nested"]]]]]]],
    }

    bounded, truncated = _bounded_value(value)

    assert truncated is True
    assert len(bounded) == len(value)
    assert len(next(key for key in bounded if key.startswith("long_key_"))) == 256


def test_t6_intentional_result_curation_is_not_marked_as_truncation(tmp_path):
    from code_governance.runtime.bounded_execution import BoundedExecution
    from code_governance.runtime.ptc import PTCCodeRuntime

    class WorkerExecutor:
        def run_ptc_worker(self, *args, **kwargs):
            return BoundedExecution("SUCCESS", 0, json.dumps({"result": {"selected": ["top", "items"]}}), "", True)

    action = Action.create_operation(OperationKind.PTC_RUN, program="result = {'selected': ['top', 'items']}")
    result = PTCCodeRuntime(executor=WorkerExecutor()).execute(action, tmp_path)

    assert result.metadata["truncated"] is False


def test_t7_confirmed_noop_and_normal_success_have_no_uncertainty(tmp_path):
    result = _executor().execute(_command("pass"), tmp_path)

    assert result.success
    assert result.effect_type == "COMMAND"
    assert result.state_changed is False
    assert result.metadata.get("effect_uncertain") is False
    assert result.metadata.get("output_partial") is False


def test_t7_completed_nonzero_command_is_not_interrupted(tmp_path):
    result = _executor().execute(_command("import sys; print('done'); sys.exit(7)"), tmp_path)

    assert result.success
    assert result.metadata["returncode"] == 7
    assert result.metadata.get("output_partial") is False
    assert result.metadata.get("effect_uncertain") is False


def test_t8_action_admission_rejection_does_not_invoke_tool_or_set_barrier(tmp_path):
    registry = ToolRegistry()
    registry.register(ShellTool())
    calls = []
    original = registry.invoke

    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    registry.invoke = counted
    invalid = Action.create_operation(OperationKind.COMMAND_RUN, argv=())
    result = CanonicalActionExecutor(registry).execute(invalid, tmp_path)

    assert not result.success
    assert calls == []
    assert result.metadata.get("effect_uncertain") is False


def test_t8_policy_approval_and_guard_rejections_do_not_invoke_tool(tmp_path):
    from code_governance.tools.guards import HardGuard
    from code_governance.runtime.execution import workspace_uncertainty_transition

    action = _command("pass")
    registry_configs = (
        ToolRegistry(policy=ToolPolicy(ToolRuntimeConfig(allowed_tools=()))),
        ToolRegistry(policy=ToolPolicy(), approval_provider=None),
        ToolRegistry(hard_guards=(HardGuard(prohibited_capabilities=("shell",)),)),
    )
    for registry in registry_configs:
        registry.register(ShellTool())
        calls = []
        invoke = registry.invoke
        registry.invoke = lambda *args, _invoke=invoke, _calls=calls, **kwargs: (
            _calls.append(args[0]), _invoke(*args, **kwargs)
        )[1]
        result = registry.execute(action, tmp_path)
        assert not result.success
        assert calls == []
        assert result.metadata.get("effect_uncertain") is False
        observation = Observation.from_execution(result, "command_result")
        assert workspace_uncertainty_transition(action, observation) is False


def _record(state, action, *, success, metadata=None, failure=None):
    state.record_action(action)
    state.record_observation(Observation(
        action_id=action.id, success=success, summary="fact",
        metadata=dict(metadata or {}), error=None if success else "failed",
        error_category=None if failure is None else failure.category,
    ))


def test_t10a_uncertainty_barrier_releases_old_read_then_new_read_is_deduplicated():
    state = AgentState.initialize("progress barrier")
    command = lambda: Action.create_operation(
        OperationKind.COMMAND_RUN, argv=(sys.executable, "-c", "print('ok')"),
    )
    before_barrier = command()
    _record(state, before_barrier, success=True, metadata={"returncode": 0, "result_fingerprint": "same"})
    uncertain = command()
    _record(state, uncertain, success=False, metadata={"effect_uncertain": True}, failure=FailureRecord("TOOL", "TOOL_TIMEOUT"))
    current = command()
    state.record_action(current)
    candidate = ExecutionResult(
        action_id=current.id, status="SUCCESS", information_gain=True,
        result_fingerprint="same", metadata={"returncode": 0},
    )

    evaluation = ProgressEvaluator().evaluate(current, candidate, state)

    assert evaluation.outcome != "NO_PROGRESS" or evaluation.evidence != "repeated_successful_verification"


def test_t10a_loop_reexecutes_read_after_timeout_then_blocks_third_duplicate(tmp_path):
    from code_governance.runtime.loop import AgentLoop
    from code_governance.tools.filesystem import FilesystemTool
    from code_governance.trace.writer import TraceManager

    (tmp_path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    registry = ToolRegistry(
        policy=ToolPolicy(ToolRuntimeConfig(shell_timeout_seconds=0.2)),
        approval_provider=_Approved(),
    )
    registry.register(FilesystemTool())
    registry.register(ShellTool())
    actions = [
        Action.create_operation(OperationKind.FILE_READ, path="a.py"),
        _command("import time; time.sleep(3)"),
        Action.create_operation(OperationKind.FILE_READ, path="a.py"),
        Action.create_operation(OperationKind.FILE_READ, path="a.py"),
    ]

    class Agent:
        def decide(self, state, context):
            return actions.pop(0)

    state = AgentState.initialize("read after uncertain command", workspace=tmp_path)
    trace = TraceManager("uncertainty-read")
    AgentLoop(Agent(), registry, trace, max_steps=4).run(state, {}, tmp_path)

    assert state.recent_observations[1].metadata["effect_uncertain"] is True
    assert state.recent_observations[2].success is True
    assert state.recent_observations[3].error_category == "REPEATED_READ_WITHOUT_NEW_EVIDENCE"
    progress = [event.data["outcome"] for event in trace.get_events() if event.type == "progress_evaluated"]
    assert progress[1] == "FAILURE"


def test_t10b_old_verification_does_not_cross_barrier_but_new_one_anchors():
    state = AgentState.initialize("verification barrier")
    command = lambda: Action.create_operation(
        OperationKind.COMMAND_RUN, argv=(sys.executable, "-c", "print('ok')"),
    )
    first = command()
    _record(state, first, success=True, metadata={"returncode": 0, "result_fingerprint": "same"})
    failed = command()
    _record(state, failed, success=False, metadata={"effect_uncertain": True}, failure=FailureRecord("TOOL", "TOOL_TIMEOUT"))
    current = command()
    state.record_action(current)
    candidate = ExecutionResult(action_id=current.id, status="SUCCESS", information_gain=True, result_fingerprint="same", metadata={"returncode": 0})
    assert ProgressEvaluator().evaluate(current, candidate, state).evidence != "repeated_successful_verification"

    state.record_observation(Observation(current.id, success=True, metadata={"returncode": 0, "result_fingerprint": "same"}))
    repeated = command()
    state.record_action(repeated)
    evaluation = ProgressEvaluator().evaluate(
        repeated,
        ExecutionResult(action_id=repeated.id, status="SUCCESS", information_gain=True, result_fingerprint="same", metadata={"returncode": 0}),
        state,
    )
    assert (evaluation.outcome, evaluation.evidence) == ("NO_PROGRESS", "repeated_successful_verification")


def test_t10c_admission_rejection_read_failure_and_noop_do_not_create_barrier():
    from code_governance.runtime.execution import last_workspace_uncertainty_index, strict_bool

    command = Action.create_operation(OperationKind.COMMAND_RUN, argv=(sys.executable, "-c", "pass"))
    read = Action.create_operation(OperationKind.FILE_READ, path="missing.py")
    entries = [
        (command, Observation(command.id, success=False, metadata={"failure_stage": "PRE_EXECUTION_GOVERNANCE", "effect_uncertain": False})),
        (read, Observation(read.id, success=False, error_category="TOOL_ERROR", metadata={"failure_stage": "TOOL_EXECUTION"})),
        (command, Observation(command.id, success=True, metadata={"effect_uncertain": False})),
    ]
    assert last_workspace_uncertainty_index(entries) == -1
    for invalid in ("true", 1, []):
        assert strict_bool({"effect_uncertain": invalid}, "effect_uncertain") is None


def test_t10d_legacy_effectful_failure_is_conservative_but_preexecution_is_not():
    from code_governance.runtime.execution import last_workspace_uncertainty_index

    command = Action.create_operation(OperationKind.COMMAND_RUN, argv=(sys.executable, "-c", "pass"))
    legacy_failed = Observation(command.id, success=False, error_category="TOOL_ERROR", metadata={"failure_stage": "TOOL_EXECUTION"})
    preexecution = Observation(command.id, success=False, error_category="POLICY_DENY", metadata={"failure_stage": "PRE_EXECUTION_GOVERNANCE"})
    assert last_workspace_uncertainty_index([(command, legacy_failed)]) == 0
    assert last_workspace_uncertainty_index([(command, preexecution)]) == -1
    for malformed in ("false", 1, []):
        observation = Observation(command.id, success=False, error_category="TOOL_ERROR", metadata={"failure_stage": "TOOL_EXECUTION", "effect_uncertain": malformed})
        assert last_workspace_uncertainty_index([(command, observation)]) == 0


def test_t10h_deterministic_rejection_cannot_use_verification_before_barrier():
    state = AgentState.initialize("rejection barrier")
    command = Action.create_operation(OperationKind.COMMAND_RUN, argv=(sys.executable, "-c", "pass"))
    _record(state, command, success=True, metadata={"returncode": 0})
    timeout = Action.create_operation(OperationKind.COMMAND_RUN, argv=(sys.executable, "-c", "pass"))
    _record(state, timeout, success=False, metadata={"effect_uncertain": True}, failure=FailureRecord("TOOL", "TOOL_TIMEOUT"))
    rejected = Action.create_operation(OperationKind.FILE_CREATE, path="new.txt", content="x")
    state.record_action(rejected)
    failure = FailureRecord("ACTION_VALIDATION", "INVALID_ACTION")
    result = ExecutionResult(action_id=rejected.id, status="FAILURE", failure=failure, metadata={"effect_uncertain": False})

    evaluation = ProgressEvaluator().evaluate(rejected, result, state)

    assert evaluation.outcome == "FAILURE"


def test_t11_qualifiers_reach_actual_codex_decision_prompt(tmp_path):
    import json
    import subprocess

    from code_governance.agent.codex_agent import CodexAgent
    from code_governance.context.builder import ContextBuilder
    from code_governance.repo.analyzer import RepoContext
    from code_governance.runtime.action_validator import ActionValidator
    from code_governance.runtime.loop import AgentLoop
    from code_governance.runtime.ptc import PTCCodeRuntime
    from code_governance.trace.writer import TraceManager

    class EmptyMemory:
        def active_rules(self):
            return ()

        def active_strategies(self):
            return ()

    context = ContextBuilder().build(
        "inspect the repository",
        RepoContext(tmp_path, (), (), (), (), {}),
        EmptyMemory(),
    )
    registry = ToolRegistry(
        policy=ToolPolicy(ToolRuntimeConfig(shell_timeout_seconds=0.2)),
        approval_provider=_Approved(),
    )
    registry.register(ShellTool())
    agent = CodexAgent(execution_mode="PTC")
    prompts = []
    payloads = [
        {
            "operation": "command.run",
            "arguments": {"argv": [sys.executable, "-c", "import sys,time; print('PROMPT_STDOUT_MARKER', flush=True); print('PROMPT_STDERR_MARKER', file=sys.stderr, flush=True); time.sleep(3)"]},
            "reason": "observe timeout evidence",
        },
        {
            "operation": "ptc.run",
            "arguments": {"program": "result = [" + ",".join(repr("x" * 100) for _ in range(50)) + "]"},
            "reason": "produce bounded-result evidence",
        },
        {"operation": "workflow.finish", "arguments": {}, "reason": "stop after evidence review"},
    ]

    def terminal_transport(command, *, prompt, workspace):
        prompts.append(prompt)
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payloads[len(prompts) - 1]), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    agent._run_cli = terminal_transport
    state = AgentState.initialize("inspect evidence", workspace=tmp_path, max_steps=3)
    AgentLoop(
        agent,
        registry,
        TraceManager("visible-evidence"),
        context_builder=ContextBuilder(),
        action_validator=ActionValidator("PTC"),
            ptc_runtime=PTCCodeRuntime(timeout_seconds=5),
        max_steps=3,
    ).run(state, context, tmp_path)

    assert len(prompts) == 3, (len(prompts), state.status, state.failure, [item.summary for item in state.recent_observations])
    assert "Effect uncertainty:" in prompts[1]
    assert "Output partial:" in prompts[1]
    assert "PROMPT_STDOUT_MARKER" in prompts[1]
    assert "PROMPT_STDERR_MARKER" in prompts[1]
    assert prompts[1].index("Effect uncertainty:") < prompts[1].index("PROMPT_STDOUT_MARKER")
    working_memory_1 = prompts[1].split("Working Memory:\n", 1)[1].split("\nActive Issues:", 1)[0]
    assert working_memory_1.index("Effect uncertainty:") < working_memory_1.index("PROMPT_STDOUT_MARKER")
    assert state.recent_observations[1].metadata["truncated"] is True
    assert "Evidence truncated:" in prompts[2]
    assert "Effect uncertainty:" in prompts[2]
    working_memory_2 = prompts[2].split("Working Memory:\n", 1)[1].split("\nActive Issues:", 1)[0]
    assert working_memory_2.index("Evidence truncated:") < working_memory_2.index("PTC curated result:")
