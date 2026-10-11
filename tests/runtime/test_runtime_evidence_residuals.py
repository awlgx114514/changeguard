import io
import json
import subprocess
import sys

import pytest

from code_governance.agent.codex_agent import CodexAgent
from code_governance.context.builder import ContextBuilder
from code_governance.repo.analyzer import RepoContext
from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.action_validator import ActionValidator
from code_governance.runtime.bounded_execution import BoundedToolExecutor
from code_governance.runtime.execution import timeout_streams
from code_governance.runtime.loop import AgentLoop
from code_governance.runtime.process_lifecycle import _BoundedCollector
from code_governance.runtime.ptc import PTCCodeRuntime
from code_governance.runtime.state import AgentState
from code_governance.tools.approval import ApprovalOutcome
from code_governance.tools.filesystem import FilesystemTool
from code_governance.tools.git import GitTool
from code_governance.tools.policy import ToolPolicy, ToolRuntimeConfig
from code_governance.tools.registry import ToolRegistry
from code_governance.tools.shell import ShellTool
from code_governance.trace.writer import TraceManager


class _Approved:
    def request(self, request):
        return ApprovalOutcome.APPROVED


class _EmptyMemory:
    def active_rules(self):
        return ()

    def active_strategies(self):
        return ()


def _shell_registry():
    registry = ToolRegistry(
        policy=ToolPolicy(ToolRuntimeConfig(shell_timeout_seconds=5)),
        approval_provider=_Approved(),
    )
    registry.register(ShellTool())
    return registry


def _run_prompt_loop(tmp_path, registry, first_payload, *, execution_mode="NATIVE", ptc_runtime=None):
    context = ContextBuilder().build(
        "exercise model-visible evidence",
        RepoContext(tmp_path, (), (), (), (), {}),
        _EmptyMemory(),
    )
    agent = CodexAgent(execution_mode=execution_mode)
    prompts = []
    payloads = [first_payload, {"operation": "workflow.finish", "arguments": {}, "reason": "done"}]

    def terminal_transport(command, *, prompt, workspace):
        prompts.append(prompt)
        output_path = command[command.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as output:
            json.dump(payloads[len(prompts) - 1], output)
        return subprocess.CompletedProcess(command, 0, "", "")

    agent._run_cli = terminal_transport
    state = AgentState.initialize("exercise model-visible evidence", workspace=tmp_path, max_steps=2)
    options = {}
    if execution_mode == "PTC":
        options["action_validator"] = ActionValidator("PTC")
    AgentLoop(
        agent,
        registry,
        TraceManager("residual-evidence-prompt"),
        context_builder=ContextBuilder(),
        ptc_runtime=ptc_runtime,
        max_steps=2,
        **options,
    ).run(state, context, tmp_path)
    return prompts, state


def test_collector_distinguishes_exact_capacity_from_one_byte_over():
    exact = _BoundedCollector(io.BytesIO(b"x" * 32), 32)
    exact.run()
    over = _BoundedCollector(io.BytesIO(b"x" * 33), 32)
    over.run()
    large = _BoundedCollector(io.BytesIO(b"x" * 32768), 32)
    large.run()

    assert exact.value() == b"x" * 32
    assert exact.omitted is False
    assert over.value() == b"x" * 32
    assert over.omitted is True
    assert large.value() == b"x" * 32
    assert large.omitted is True


def test_owned_process_reports_capture_omission_per_stream(tmp_path):
    execution = BoundedToolExecutor(max_output_bytes=32).run_subprocess(
        [sys.executable, "-c", "print('x' * 32, end='')"],
        tmp_path,
        timeout_seconds=3,
        action_id="exact-cap",
    )
    assert execution.stdout == "x" * 32
    assert execution.stdout_omitted is False
    assert execution.stderr_omitted is False

    execution = BoundedToolExecutor(max_output_bytes=32).run_subprocess(
        [sys.executable, "-c", "print('x' * 33, end='')"],
        tmp_path,
        timeout_seconds=3,
        action_id="one-over",
    )
    assert execution.stdout == "x" * 32
    assert execution.stdout_omitted is True


def test_ptc_parent_preserves_child_capture_omission(tmp_path):
    command = (
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('x' * 227)",
    )
    registry = ToolRegistry()
    registry.register(GitTool(
        executor=BoundedToolExecutor(max_output_bytes=32),
        command_prefix=command,
    ))

    class WorkerExecutor:
        def run_ptc_worker(self, *args, **kwargs):
            kwargs["request_handler"]({"name": "git_diff", "args": []})
            return type("BoundedExecution", (), {
                "status": "SUCCESS", "returncode": 0,
                "stdout": json.dumps({"result": {"ok": True}}),
                "stderr": "", "quiescent": True, "error": None,
            })()

    action = Action.create_operation(OperationKind.PTC_RUN, program="result = tools.git_diff()")
    result = PTCCodeRuntime(tool_registry=registry, executor=WorkerExecutor()).execute(action, tmp_path)

    assert result.success
    assert result.metadata["truncated"] is True


def test_ptc_capture_omission_qualifier_reaches_real_next_decision_prompt(tmp_path):
    command = (
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('x' * 227)",
    )
    registry = ToolRegistry()
    registry.register(GitTool(
        executor=BoundedToolExecutor(max_output_bytes=32),
        command_prefix=command,
    ))

    class WorkerExecutor:
        def run_ptc_worker(self, *args, **kwargs):
            kwargs["request_handler"]({"name": "git_diff", "args": []})
            return type("BoundedExecution", (), {
                "status": "SUCCESS", "returncode": 0,
                "stdout": json.dumps({"result": {"ok": True}}),
                "stderr": "", "quiescent": True, "error": None,
            })()

    payload = {
        "operation": "ptc.run",
        "arguments": {"program": "result = tools.git_diff()"},
        "reason": "inspect bounded child output",
    }
    prompts, state = _run_prompt_loop(
        tmp_path,
        registry,
        payload,
        execution_mode="PTC",
        ptc_runtime=PTCCodeRuntime(tool_registry=registry, executor=WorkerExecutor()),
    )

    assert len(prompts) == 2
    assert state.recent_observations[0].metadata["truncated"] is True
    assert "Evidence truncated:" in prompts[1]


class _RecordingBoundedExecutor(BoundedToolExecutor):
    def __init__(self, *, max_output_bytes=32):
        super().__init__(max_output_bytes=max_output_bytes)
        self.omission_facts = []

    def run_subprocess(self, *args, omission_callback=None, **kwargs):
        def record(stdout_omitted, stderr_omitted):
            self.omission_facts.append((stdout_omitted, stderr_omitted))
            if omission_callback is not None:
                omission_callback(stdout_omitted, stderr_omitted)

        return super().run_subprocess(*args, omission_callback=record, **kwargs)


def _run_ptc_failure_prompt(tmp_path, registry, program):
    payload = {
        "operation": "ptc.run",
        "arguments": {"program": program},
        "reason": "inspect a bounded child failure",
    }
    return _run_prompt_loop(
        tmp_path,
        registry,
        payload,
        execution_mode="PTC",
        ptc_runtime=PTCCodeRuntime(tool_registry=registry),
    )


def _record_registry_failures(registry):
    failures = []
    original = registry._failure_result

    def record(*args, **kwargs):
        result = original(*args, **kwargs)
        failures.append(result)
        return result

    registry._failure_result = record
    return failures


def test_filesystem_parse_failure_preserves_real_capture_omission_to_prompt(tmp_path):
    for index in range(12):
        (tmp_path / f"target_{index:02d}.py").write_text("# fixture\n", encoding="utf-8")
    executor = _RecordingBoundedExecutor()
    registry = ToolRegistry()
    registry.register(FilesystemTool(executor=executor))
    registry.register(GitTool(executor=executor))
    failures = _record_registry_failures(registry)

    prompts, state = _run_ptc_failure_prompt(
        tmp_path,
        registry,
        'result = tools.search_files("target_")',
    )

    assert executor.omission_facts == [(True, False)]
    assert len(failures) == 1
    assert failures[0].metadata.get("truncated") is True
    assert len(prompts) == 2
    assert state.recent_observations[0].metadata["truncated"] is True
    assert "Evidence truncated:" in prompts[1]


def test_git_timeout_preserves_real_capture_omission_to_prompt(tmp_path):
    executor = _RecordingBoundedExecutor()
    command = (
        sys.executable,
        "-c",
        "import sys,time; sys.stdout.write('G'*229); sys.stdout.flush(); time.sleep(2)",
    )
    registry = ToolRegistry(bounded_timeout_seconds=0.25)
    registry.register(FilesystemTool(executor=executor))
    registry.register(GitTool(executor=executor, command_prefix=command))
    failures = _record_registry_failures(registry)

    prompts, state = _run_ptc_failure_prompt(
        tmp_path,
        registry,
        "result = tools.git_diff()",
    )

    assert executor.omission_facts == [(True, False)]
    assert len(failures) == 1
    assert failures[0].metadata.get("truncated") is True
    assert len(prompts) == 2
    assert state.recent_observations[0].metadata["truncated"] is True
    assert "Evidence truncated:" in prompts[1]


def test_short_failure_without_capture_omission_stays_untruncated(tmp_path):
    executor = _RecordingBoundedExecutor()
    registry = ToolRegistry()
    registry.register(FilesystemTool(executor=executor))
    registry.register(GitTool(executor=executor))
    failures = _record_registry_failures(registry)

    prompts, state = _run_ptc_failure_prompt(tmp_path, registry, 'result = tools.search_files("")')

    assert failures
    assert not executor.omission_facts
    assert state.recent_observations[0].metadata.get("truncated", False) is False
    assert "Evidence truncated:" not in prompts[1]


def test_timeout_without_capture_omission_stays_untruncated(tmp_path):
    executor = _RecordingBoundedExecutor()
    command = (sys.executable, "-c", "import time; time.sleep(2)")
    registry = ToolRegistry(bounded_timeout_seconds=0.2)
    registry.register(FilesystemTool(executor=executor))
    registry.register(GitTool(executor=executor, command_prefix=command))
    failures = _record_registry_failures(registry)

    prompts, state = _run_ptc_failure_prompt(tmp_path, registry, "result = tools.git_diff()")

    assert failures
    assert executor.omission_facts == [(False, False)]
    assert state.recent_observations[0].metadata.get("truncated", False) is False
    assert "Evidence truncated:" not in prompts[1]


def test_parse_failure_without_capture_omission_stays_untruncated(tmp_path):
    executor = _RecordingBoundedExecutor()
    registry = ToolRegistry()
    registry.register(FilesystemTool(
        executor=executor,
        search_worker_command=(sys.executable, "-c", "print('{')"),
    ))
    registry.register(GitTool(executor=executor))
    failures = _record_registry_failures(registry)

    prompts, state = _run_ptc_failure_prompt(
        tmp_path, registry, 'result = tools.search_files("target_")',
    )

    assert failures
    assert executor.omission_facts == [(False, False)]
    assert state.recent_observations[0].metadata.get("truncated", False) is False
    assert "Evidence truncated:" not in prompts[1]


def test_intentional_ptc_result_curation_is_not_capture_truncation(tmp_path):
    for index in range(3):
        (tmp_path / f"target_{index:02d}.py").write_text("# fixture\n", encoding="utf-8")
    executor = _RecordingBoundedExecutor(max_output_bytes=4096)
    registry = ToolRegistry()
    registry.register(FilesystemTool(executor=executor))
    registry.register(GitTool(executor=executor))

    prompts, state = _run_ptc_failure_prompt(
        tmp_path, registry, 'result = tools.search_files("target_")',
    )

    assert executor.omission_facts == [(False, False)]
    assert state.recent_observations[0].metadata.get("truncated", False) is False
    assert "Evidence truncated:" not in prompts[1]


@pytest.mark.parametrize(
    ("source", "visible", "omission_expected"),
    [
        (
            "import sys; print('SHORT_STDOUT_MARKER'); print('SHORT_STDERR_MARKER', file=sys.stderr)",
            ("SHORT_STDOUT_MARKER", "SHORT_STDERR_MARKER"), False,
        ),
        (
            "import sys; print('O' * 12000); print('IMPORTANT_STDERR_MARKER', file=sys.stderr)",
            ("stderr:", "IMPORTANT_STDERR_MARKER"), True,
        ),
        (
            "import sys; print('O' * 12000); print('E' * 9000, file=sys.stderr)",
            ("stdout:", "stderr:"), True,
        ),
        (
            "print('O' * 12000)",
            ("stdout:",), True,
        ),
    ],
    ids=("short-both-streams", "long-stdout-short-stderr", "long-both-streams", "long-stdout-only"),
)
def test_normal_command_output_completeness_is_visible_in_next_model_prompt(
    tmp_path, source, visible, omission_expected,
):
    payload = {
        "operation": "command.run",
        "arguments": {"argv": [sys.executable, "-c", source]},
        "reason": "inspect completed command output",
    }
    prompts, state = _run_prompt_loop(tmp_path, _shell_registry(), payload)

    assert len(prompts) == 2
    assert all(marker in prompts[1] for marker in visible)
    if omission_expected:
        assert "Evidence truncated:" in prompts[1]
        assert state.recent_observations[0].metadata["truncated"] is True
    else:
        assert "Evidence truncated:" not in prompts[1]
        assert state.recent_observations[0].metadata["truncated"] is False
    assert "Output partial:" not in prompts[1]


def test_normal_command_output_omission_is_visible_in_next_model_prompt(tmp_path):
    source = "import sys; print('O' * 12000); print('IMPORTANT_STDERR_MARKER', file=sys.stderr)"
    context = ContextBuilder().build(
        "inspect command output",
        RepoContext(tmp_path, (), (), (), (), {}),
        _EmptyMemory(),
    )
    registry = ToolRegistry(
        policy=ToolPolicy(ToolRuntimeConfig(shell_timeout_seconds=5)),
        approval_provider=_Approved(),
    )
    registry.register(ShellTool())
    agent = CodexAgent()
    prompts = []
    payloads = [
        {
            "operation": "command.run",
            "arguments": {"argv": [
                sys.executable, "-c",
                source,
            ]},
            "reason": "inspect both completed output streams",
        },
        {"operation": "workflow.finish", "arguments": {}, "reason": "finish inspection"},
    ]

    def terminal_transport(command, *, prompt, workspace):
        prompts.append(prompt)
        output_path = command[command.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as output:
            json.dump(payloads[len(prompts) - 1], output)
        return subprocess.CompletedProcess(command, 0, "", "")

    agent._run_cli = terminal_transport
    state = AgentState.initialize("inspect command output", workspace=tmp_path, max_steps=2)
    AgentLoop(
        agent,
        registry,
        TraceManager("normal-output-omission"),
        context_builder=ContextBuilder(),
        max_steps=2,
    ).run(state, context, tmp_path)

    assert len(prompts) == 2
    assert "IMPORTANT_STDERR_MARKER" in prompts[1] or "Evidence truncated:" in prompts[1]
    assert "Output partial:" not in prompts[1]


def test_history_prompt_keeps_confirmed_change_and_output_omission_qualifiers(tmp_path):
    from code_governance.runtime.execution import ExecutionResult
    from code_governance.runtime.history import HistoryManager
    from code_governance.runtime.observation import Observation

    state = AgentState.initialize("review complete evidence", workspace=tmp_path)
    action = Action.create_operation(OperationKind.COMMAND_RUN, argv=(sys.executable, "-c", "pass"))
    state.record_action(action)
    result = ExecutionResult(
        action_id=action.id,
        status="SUCCESS",
        state_changed=True,
        metadata={"effect_uncertain": True, "truncated": True},
    )
    observation = Observation.from_execution(result, "command_result")
    state.record_observation(observation)
    HistoryManager().record(state, action, observation)
    context = ContextBuilder().build(
        "review complete evidence",
        RepoContext(tmp_path, (), (), (), (), {}),
        _EmptyMemory(),
        agent_state=state,
    )
    agent = CodexAgent()
    prompts = []

    def terminal_transport(command, *, prompt, workspace):
        prompts.append(prompt)
        output_path = command[command.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as output:
            json.dump({"operation": "workflow.finish", "arguments": {}, "reason": "done"}, output)
        return subprocess.CompletedProcess(command, 0, "", "")

    agent._run_cli = terminal_transport
    agent.decide(state, context)

    assert "Effect uncertainty: a partial change is confirmed" in prompts[0]
    assert "Evidence truncated:" in prompts[0]


def test_timeout_streams_reject_arbitrary_exception_wrappers():
    timeout = subprocess.TimeoutExpired(["tool"], 1, output=b"partial", stderr=b"diagnostic")

    supported = TimeoutError("supported shell timeout")
    supported.__cause__ = timeout
    assert timeout_streams(supported) == ("partial", "diagnostic")

    for error in (ValueError("bad value"), RuntimeError("unrelated failure")):
        error.__cause__ = timeout
        assert timeout_streams(error) is None


def test_timeout_streams_do_not_follow_nested_causes_or_context():
    timeout = subprocess.TimeoutExpired(["tool"], 1, output=b"nested", stderr=b"nested-error")
    middle = ValueError("middle")
    middle.__cause__ = timeout
    supported = TimeoutError("supported wrapper")
    supported.__cause__ = middle
    assert timeout_streams(supported) is None

    contextual = TimeoutError("supported wrapper")
    contextual.__context__ = timeout
    assert timeout_streams(contextual) is None


def test_timeout_streams_ignore_output_on_unsupported_wrapper():
    timeout = subprocess.TimeoutExpired(["tool"], 1, output=b"cause", stderr=b"cause-error")
    error = ValueError("not a timeout")
    error.stdout = b"wrapper-output"
    error.stderr = b"wrapper-error"
    error.__cause__ = timeout

    assert timeout_streams(error) is None
