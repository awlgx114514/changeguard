import json
from pathlib import Path
import subprocess

import pytest

from code_governance.agent.codex_agent import CodexAgent
from code_governance.agent.planner import Plan
from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.observation import Observation
from code_governance.runtime.state import AgentState
from code_governance.contract.schema import (
    ChangeContract,
    Constraints,
    Expected,
    Intent,
    Metadata,
    Verification,
    VerificationClaim,
)


def contract() -> ChangeContract:
    return ChangeContract(
        metadata=Metadata(project="demo"),
        intent=Intent(goal="update one file"),
        expected=Expected(files=["app.py"], change_level="L1"),
        constraints=Constraints(forbidden=["settings.py"]),
        verification=Verification(claims=[VerificationClaim(id="unit_tests", description="Unit tests pass.", request={"argv": ("python", "-m", "unittest")})]),
    )


def test_codex_agent_rejects_legacy_direct_execution(monkeypatch):
    monkeypatch.setattr(
        "code_governance.agent.codex_agent.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "done", ""),
    )
    with pytest.raises(RuntimeError, match="canonical Action loop"):
        CodexAgent().run("update the application", contract(), workspace=Path("."))


def test_codex_agent_decides_a_serialized_runtime_action(monkeypatch, tmp_path):
    captured = {}

    def fake_run(*args, **kwargs):
        captured["prompt"] = kwargs["input"]
        output_path = Path(args[0][args[0].index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(_finish_action()),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args[0],
            0,
            "",
            "",
        )

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)
    state = AgentState.initialize("update app", {"version": "v1"}, {"steps": []}, tmp_path)

    action = CodexAgent().decide(state, {"version": "v1"})

    assert action.operation.kind is OperationKind.WORKFLOW_FINISH
    assert "conforming to the supplied Action schema" in captured["prompt"]


def test_codex_agent_reads_runtime_action_from_cli_output_file(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        command = args[0]
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(_finish_action()),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)
    state = AgentState.initialize("update app", {"version": "v1"}, {"steps": []}, tmp_path)

    action = CodexAgent().decide(state, {"version": "v1"})

    assert action.operation.kind is OperationKind.WORKFLOW_FINISH


def test_diagnostic_callback_observes_boundaries_without_changing_action(monkeypatch, tmp_path):
    events = []

    def fake_run(*args, **kwargs):
        command = args[0]
        _output_path(command).write_text(json.dumps(_finish_action()), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "cli diagnostic", "")

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)
    agent = CodexAgent()
    agent._diagnostic_callback = lambda event, **facts: events.append((event, facts))

    action = agent.decide(_state(tmp_path), {"version": "v1"})

    assert action.operation.kind is OperationKind.WORKFLOW_FINISH
    assert [event for event, _ in events] == [
        "assistant_output_artifact_checked", "action_parse_started", "action_parsed",
    ]
    assert events[0][1]["artifact_present"] is True
    assert events[2][1]["operation"] == "workflow.finish"


def test_communicate_path_reports_only_observable_process_boundaries(monkeypatch, tmp_path):
    class Process:
        pid = 4242
        returncode = 0

        def communicate(self, input=None, timeout=None):
            assert input == "decision prompt"
            assert timeout == 7
            return ('{"type":"turn.completed"}', "retry: transport fallback")

    class ProcessTree:
        def close(self):
            pass

    events = []
    module = __import__("code_governance.agent.codex_agent", fromlist=["CodexAgent"])
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(module, "_create_windows_process_tree", lambda _process: ProcessTree())
    agent = CodexAgent(timeout=7)
    agent._diagnostic_callback = lambda event, **facts: events.append((event, facts))

    result = agent._run_cli(["codex", "exec"], prompt="decision prompt", workspace=tmp_path)

    names = [event for event, _ in events]
    assert result.returncode == 0
    assert names == [
        "process_started", "communicate_started", "process_exited", "stdin_completed",
        "pipe_eof_stdout", "pipe_eof_stderr", "communication_completed", "cleanup_completed",
    ]
    assert "first_stdout_observed" not in names
    assert "first_stderr_observed" not in names


def test_assistant_output_can_be_observed_before_action_parse_fails(monkeypatch, tmp_path):
    events = []

    def fake_run(*args, **kwargs):
        command = args[0]
        _output_path(command).write_text('{"not":"an action"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "assistant output exists", "")

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)
    agent = CodexAgent()
    agent._diagnostic_callback = lambda event, **facts: events.append((event, facts))
    with pytest.raises(RuntimeError):
        agent.decide(_state(tmp_path), {"version": "v1"})

    names = [event for event, _ in events]
    assert names == ["assistant_output_artifact_checked", "action_parse_started"]
    assert events[0][1]["artifact_present"] is True
    assert "action_parsed" not in names


def _finish_action():
    return {
        "operation": "workflow.finish",
        "arguments": {},
        "reason": "done",
    }


def _state(tmp_path):
    return AgentState.initialize("update app", {"version": "v1"}, {"steps": []}, tmp_path)


def _output_path(command):
    return Path(command[command.index("--output-last-message") + 1])


def test_codex_agent_uses_structured_exec_protocol(monkeypatch, tmp_path):
    captured = {}

    def fake_run(*args, **kwargs):
        captured["command"] = args[0]
        captured["kwargs"] = kwargs
        schema_path = Path(args[0][args[0].index("--output-schema") + 1])
        captured["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        _output_path(args[0]).write_text(json.dumps(_finish_action()), encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, "jsonl diagnostics", "")

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)

    action = CodexAgent().decide(_state(tmp_path), {"version": "v1"})

    command = captured["command"]
    assert action.operation.kind is OperationKind.WORKFLOW_FINISH
    assert "--json" in command
    assert "--output-schema" in command
    assert "--output-last-message" in command
    assert command[-1] == "-"
    assert "conforming to the supplied Action schema" in captured["kwargs"]["input"]
    assert "operation" in captured["schema"]["properties"]
    assert "oneOf" not in captured["schema"]


def test_codex_agent_excludes_write_after_a_completed_write(monkeypatch, tmp_path):
    captured = {}

    def fake_run(*args, **kwargs):
        command = args[0]
        schema_path = Path(command[command.index("--output-schema") + 1])
        captured["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        _output_path(command).write_text(json.dumps(_finish_action()), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)
    state = _state(tmp_path)
    action = Action.create_operation(OperationKind.FILE_CREATE, path="app.py", content="updated")
    state.record_action(action)
    state.record_observation(Observation(action_id=action.id, success=True))

    CodexAgent().decide(state, {"version": "v1"})

    allowed = captured["schema"]["properties"]["operation"]["enum"]
    assert "file.write" in allowed


def test_codex_agent_decision_prompt_defines_single_file_write_semantics(tmp_path):
    prompt = CodexAgent()._build_decision_prompt(_state(tmp_path), {"version": "v1"})

    assert "conforming to the supplied Action schema" in prompt
    assert "input.action=search_file" not in prompt
    assert "small workspace-local script" not in prompt


def test_codex_agent_prompt_does_not_define_a_second_operation_protocol(tmp_path):
    prompt = CodexAgent()._build_decision_prompt(_state(tmp_path), {"version": "v1"})

    assert "Set every field not used by the selected operation to null." not in prompt
    assert "arguments only for the selected operation" in prompt


def test_codex_agent_decision_prompt_does_not_redefine_runtime_protocol(tmp_path):
    prompt = CodexAgent()._build_decision_prompt(_state(tmp_path), {"version": "v1"})

    assert "input.action=search_file" not in prompt
    assert "small workspace-local script" not in prompt
    assert "workspace-relative file path" not in prompt


def test_codex_agent_rejects_nonzero_structured_cli_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "code_governance.agent.codex_agent.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 9, "", "request failed"),
    )

    with pytest.raises(RuntimeError, match="codex_cli_nonzero_exit"):
        CodexAgent().decide(_state(tmp_path), {"version": "v1"})


def test_codex_agent_rejects_structured_cli_timeout(monkeypatch, tmp_path):
    class TimedOutProcess:
        pid = 4242

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired("codex", timeout)

        def poll(self):
            return 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    class ProcessTree:
        def terminate(self):
            pass

        def close(self):
            pass

    module = __import__("code_governance.agent.codex_agent", fromlist=["CodexAgent"])
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: TimedOutProcess())
    monkeypatch.setattr(module, "_create_windows_process_tree", lambda process: ProcessTree())

    with pytest.raises(RuntimeError, match="codex_cli_timeout"):
        CodexAgent(timeout=5).decide(_state(tmp_path), {"version": "v1"})


def test_codex_agent_timeout_terminates_the_owned_windows_process_tree(monkeypatch, tmp_path):
    class TimedOutProcess:
        pid = 4242

        def __init__(self):
            self.communicate_calls = []

        def communicate(self, input=None, timeout=None):
            self.communicate_calls.append((input, timeout))
            if len(self.communicate_calls) == 1:
                raise subprocess.TimeoutExpired("codex", timeout, output='{"partial": true}')
            return ('{"partial": true}', "")

        def poll(self):
            return 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    class ProcessTree:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def close(self):
            pass

    process = TimedOutProcess()
    tree = ProcessTree()
    module = __import__("code_governance.agent.codex_agent", fromlist=["CodexAgent"])
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    monkeypatch.setattr(module, "_create_windows_process_tree", lambda process: tree, raising=False)

    with pytest.raises(RuntimeError, match="codex_cli_timeout"):
        CodexAgent(timeout=1).decide(_state(tmp_path), {"version": "v1"})

    assert process.communicate_calls[0][1] == 1
    assert tree.terminated is True
    assert len(process.communicate_calls) == 2


def test_codex_agent_timeout_escalates_from_root_termination_to_tree_termination(monkeypatch, tmp_path):
    class RefusingProcess:
        pid = 4242

        def __init__(self):
            self.terminated = False
            self.wait_calls = []
            self.communicate_calls = 0

        def communicate(self, input=None, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired("codex", timeout)
            return ("", "")

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            raise subprocess.TimeoutExpired("codex", timeout)

        def poll(self):
            return None

    class ProcessTree:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def close(self):
            pass

    process = RefusingProcess()
    tree = ProcessTree()
    module = __import__("code_governance.agent.codex_agent", fromlist=["CodexAgent"])
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(module, "_create_windows_process_tree", lambda process: tree, raising=False)

    with pytest.raises(RuntimeError, match="codex_cli_timeout"):
        CodexAgent(timeout=1).decide(_state(tmp_path), {"version": "v1"})

    assert process.terminated is True
    assert process.wait_calls
    assert tree.terminated is True


def test_codex_agent_timeout_closes_pipes_after_bounded_cleanup(monkeypatch, tmp_path):
    class Stream:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class PipeHoldingProcess:
        pid = 4242

        def __init__(self):
            self.stdin, self.stdout, self.stderr = Stream(), Stream(), Stream()
            self.communicate_calls = 0
            self.wait_calls = []

        def communicate(self, input=None, timeout=None):
            self.communicate_calls += 1
            raise subprocess.TimeoutExpired("codex", timeout)

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if len(self.wait_calls) == 1:
                raise subprocess.TimeoutExpired("codex", timeout)
            return 0

    class ProcessTree:
        def terminate(self):
            pass

        def close(self):
            pass

    process = PipeHoldingProcess()
    module = __import__("code_governance.agent.codex_agent", fromlist=["CodexAgent"])
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(module, "_create_windows_process_tree", lambda process: ProcessTree())

    with pytest.raises(RuntimeError, match="codex_cli_timeout"):
        CodexAgent(timeout=1).decide(_state(tmp_path), {"version": "v1"})

    assert process.stdin.closed is True
    assert process.stdout.closed is True
    assert process.stderr.closed is True
    assert process.wait_calls[-1] == module._FINAL_WAIT_SECONDS


def test_codex_agent_with_timeout_returns_a_complete_structured_action(monkeypatch, tmp_path):
    captured = {}

    class CompletedProcess:
        pid = 4242
        returncode = 0

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            captured["timeout"] = timeout
            _output_path(captured["command"]).write_text(
                json.dumps(_finish_action()), encoding="utf-8"
            )
            return ("jsonl diagnostics", "")

    class ProcessTree:
        def terminate(self):
            raise AssertionError("normal completion must not terminate the process tree")

        def close(self):
            captured["tree_closed"] = True

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["popen_kwargs"] = kwargs
        return CompletedProcess()

    module = __import__("code_governance.agent.codex_agent", fromlist=["CodexAgent"])
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module, "_create_windows_process_tree", lambda process: ProcessTree())

    action = CodexAgent(timeout=5).decide(_state(tmp_path), {"version": "v1"})

    assert action.operation.kind is OperationKind.WORKFLOW_FINISH
    assert captured["timeout"] == 5
    assert captured["popen_kwargs"]["stdin"] is module.subprocess.PIPE
    assert captured["tree_closed"] is True


def test_codex_agent_rejects_missing_structured_output_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "code_governance.agent.codex_agent.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    with pytest.raises(RuntimeError, match="codex_decision_output_missing"):
        CodexAgent().decide(_state(tmp_path), {"version": "v1"})


def test_codex_agent_rejects_empty_structured_output_file(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        _output_path(args[0]).write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="codex_decision_output_empty"):
        CodexAgent().decide(_state(tmp_path), {"version": "v1"})


def test_codex_agent_rejects_invalid_structured_output_json(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        _output_path(args[0]).write_text("{", encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="codex_decision_invalid_json"):
        CodexAgent().decide(_state(tmp_path), {"version": "v1"})


def test_codex_agent_rejects_structured_output_missing_required_fields(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        _output_path(args[0]).write_text('{"id": "action-1"}', encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="codex_decision_schema_invalid"):
        CodexAgent().decide(_state(tmp_path), {"version": "v1"})


def test_codex_agent_rejects_unconvertible_structured_action(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        payload = _finish_action()
        payload["operation"] = "unknown.operation"
        _output_path(args[0]).write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="codex_decision_transport_invalid"):
        CodexAgent().decide(_state(tmp_path), {"version": "v1"})


def test_codex_agent_accepts_stderr_when_structured_output_is_valid(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        _output_path(args[0]).write_text(json.dumps(_finish_action()), encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, "jsonl diagnostics", "transport warning")

    monkeypatch.setattr("code_governance.agent.codex_agent.subprocess.run", fake_run)

    assert CodexAgent().decide(_state(tmp_path), {"version": "v1"}).operation.kind is OperationKind.WORKFLOW_FINISH
