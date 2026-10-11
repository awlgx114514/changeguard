"""Counterexamples for provider cleanup facts; only the OS process is mocked."""

import subprocess

import pytest

from code_governance.agent import codex_agent as module
from code_governance.agent.codex_agent import CodexAgent, CodexDecisionError
from code_governance.runtime.state import AgentState


class _Stream:
    def close(self):
        pass


class _TimeoutProcess:
    pid = 4321
    returncode = None

    def __init__(self, branch):
        self.branch = branch
        self.communicates = 0
        self.waits = 0
        self.stdin, self.stdout, self.stderr = _Stream(), _Stream(), _Stream()
        self.original_timeout = subprocess.TimeoutExpired(
            "mock.exe", 7, output=b"initial", stderr=b"old"
        )

    def communicate(self, input=None, timeout=None):
        self.communicates += 1
        if self.communicates == 1:
            raise self.original_timeout
        if self.branch == "complete":
            self.returncode = -1
            return "initial+cleanup-new", "provider API HTTP 503 Service Unavailable"
        raise subprocess.TimeoutExpired(
            "mock.exe", timeout,
            output=b"initial+cleanup-new",
            stderr=b"provider API HTTP 503 Service Unavailable",
        )

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        self.waits += 1
        if self.waits == 1 or self.branch == "wait_timeout":
            raise subprocess.TimeoutExpired("mock.exe", timeout)
        if self.branch == "wait_error":
            raise OSError("cleanup wait failed")
        self.returncode = -1
        return -1


class _Tree:
    def __init__(self, *, terminate_error=False, close_error=False):
        self.terminate_error = terminate_error
        self.close_error = close_error

    def terminate(self):
        if self.terminate_error:
            raise OSError("cleanup termination failed")

    def close(self):
        if self.close_error:
            raise OSError("cleanup close failed")


def _decide_timeout(monkeypatch, tmp_path, branch, *, tree=None, diagnostic_failure=False):
    process = _TimeoutProcess(branch)
    events = []
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(module, "_create_windows_process_tree", lambda _process: tree or _Tree())
    monkeypatch.setattr(CodexAgent, "_executable_command", lambda _self: ["mock.exe"])
    agent = CodexAgent(timeout=7)

    def record(boundary, **facts):
        events.append((boundary, facts))
        if diagnostic_failure and boundary in {
            "cleanup_output_observed", "process_exited", "termination_completed", "cleanup_completed"
        }:
            raise OSError("diagnostic persistence failed")

    agent._diagnostic_callback = record
    state = AgentState.initialize("inspect app", {"version": "v1"}, {"steps": []}, tmp_path)
    with pytest.raises(CodexDecisionError) as captured:
        agent.decide(state, {"version": "v1"})
    return captured.value, process.original_timeout, events


def _facts(events, boundary):
    return [facts for name, facts in events if name == boundary]


@pytest.mark.parametrize("branch", ["complete", "final_wait"])
def test_timeout_preserves_earlier_snapshot_and_adds_new_cleanup_facts(monkeypatch, tmp_path, branch):
    # Losing the cleanup communicate result or final wait exit must fail here.
    error, original_timeout, events = _decide_timeout(monkeypatch, tmp_path, branch)
    assert error.category == "codex_cli_timeout"
    assert error.__cause__ is original_timeout
    assert original_timeout.output == b"initial"
    assert original_timeout.stderr == b"old"
    assert _facts(events, "deadline_fired") == [{
        "timeout_seconds": 7, "stdout": b"initial", "stderr": b"old",
    }]
    cleanup = _facts(events, "cleanup_output_observed")
    assert len(cleanup) == 1
    expected_stdout = "initial+cleanup-new" if branch == "complete" else b"initial+cleanup-new"
    expected_stderr = (
        "provider API HTTP 503 Service Unavailable"
        if branch == "complete" else b"provider API HTTP 503 Service Unavailable"
    )
    assert cleanup[0] == {
        "stdout": expected_stdout, "stderr": expected_stderr,
        "streams_complete": branch == "complete",
    }
    assert _facts(events, "process_exited") == [{"returncode": -1}]
    assert _facts(events, "termination_completed") == [{"returncode": -1}]
    names = [name for name, _ in events]
    assert names.index("deadline_fired") < names.index("cleanup_output_observed")
    assert names.index("cleanup_output_observed") < names.index("process_exited")
    assert names.index("process_exited") < names.index("termination_completed")
    if branch == "final_wait":
        assert not _facts(events, "pipe_eof_stdout")
        assert not _facts(events, "pipe_eof_stderr")


@pytest.mark.parametrize("branch", ["wait_error", "wait_timeout", "tree_error"])
def test_cleanup_failure_keeps_primary_timeout_without_fabricating_final_exit(monkeypatch, tmp_path, branch):
    tree = _Tree(terminate_error=branch == "tree_error")
    error, original_timeout, events = _decide_timeout(monkeypatch, tmp_path, branch, tree=tree)
    assert error.category == "codex_cli_timeout"
    assert error.__cause__ is original_timeout
    assert not _facts(events, "process_exited")
    assert not _facts(events, "termination_completed")
    failure = _facts(events, "termination_failed")
    assert len(failure) == 1
    assert failure[0]["error_type"] == ("TimeoutExpired" if branch == "wait_timeout" else "OSError")
    assert failure[0]["detail"]
    if branch == "tree_error":
        assert not _facts(events, "cleanup_output_observed")
    else:
        cleanup = _facts(events, "cleanup_output_observed")
        assert cleanup == [{
            "stdout": b"initial+cleanup-new",
            "stderr": b"provider API HTTP 503 Service Unavailable",
            "streams_complete": False,
        }]


@pytest.mark.parametrize("branch", ["complete", "final_wait"])
def test_cleanup_diagnostic_persistence_failure_cannot_replace_primary_timeout(monkeypatch, tmp_path, branch):
    error, original_timeout, events = _decide_timeout(
        monkeypatch, tmp_path, branch, diagnostic_failure=True,
    )
    assert error.category == "codex_cli_timeout"
    assert error.__cause__ is original_timeout
    assert _facts(events, "cleanup_output_observed")
    assert _facts(events, "process_exited") == [{"returncode": -1}]


def test_cleanup_close_failure_keeps_primary_timeout_and_reports_secondary_failure(monkeypatch, tmp_path):
    error, original_timeout, events = _decide_timeout(
        monkeypatch, tmp_path, "complete", tree=_Tree(close_error=True),
    )
    assert error.category == "codex_cli_timeout"
    assert error.__cause__ is original_timeout
    assert _facts(events, "process_exited") == [{"returncode": -1}]
    failure = _facts(events, "cleanup_failed")
    assert failure == [{"error_type": "OSError", "detail": "cleanup close failed"}]
    assert not _facts(events, "cleanup_completed")

