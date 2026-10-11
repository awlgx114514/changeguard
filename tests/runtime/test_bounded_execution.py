import os
import sys
import time

from code_governance.runtime.bounded_execution import BoundedToolExecutor
from code_governance.trace.writer import TraceManager


def _sleep_command(seconds: float):
    return [sys.executable, "-c", f"import time; print('ready'); time.sleep({seconds})"]


def test_bounded_executor_returns_success_after_process_quiescence(tmp_path):
    trace = TraceManager("bounded-success")
    result = BoundedToolExecutor().run_subprocess(
        _sleep_command(0),
        tmp_path,
        timeout_seconds=2,
        action_id="action-success",
        trace=trace,
    )

    assert result.status == "SUCCESS"
    assert result.quiescent is True
    assert result.returncode == 0
    assert result.stdout == "ready\n"
    assert [event.type for event in trace.get_events()] == [
        "tool_execution_started",
        "tool_quiescent",
        "tool_execution_completed",
    ]


def test_bounded_executor_timeout_is_reported_only_after_quiescence(tmp_path):
    trace = TraceManager("bounded-timeout")
    started = time.monotonic()
    result = BoundedToolExecutor().run_subprocess(
        _sleep_command(30),
        tmp_path,
        timeout_seconds=0.2,
        action_id="action-timeout",
        trace=trace,
    )

    assert result.status == "TIMEOUT"
    assert result.quiescent is True
    assert time.monotonic() - started < 3
    assert [event.type for event in trace.get_events()] == [
        "tool_execution_started",
        "tool_timeout_requested",
        "tool_termination_started",
        "tool_quiescent",
        "tool_execution_completed",
    ]


def test_repeated_bounded_timeouts_do_not_leave_processes(tmp_path):
    executor = BoundedToolExecutor()
    for index in range(3):
        result = executor.run_subprocess(
            _sleep_command(30),
            tmp_path,
            timeout_seconds=0.1,
            action_id=f"action-{index}",
            trace=TraceManager(f"bounded-repeat-{index}"),
        )
        assert result.status == "TIMEOUT"
        assert result.quiescent is True
