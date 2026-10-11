"""The narrow Runtime-owned executor for future read-only PTC capabilities."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence
import json
import queue
import subprocess
from threading import Thread
from time import monotonic

from .process_lifecycle import OwnedProcess, ProcessLifecycleError, _create_process_tree, _terminate_owned, _wait_for_quiescence, _PIPE_CLEANUP_SECONDS, _FINAL_WAIT_SECONDS


@dataclass(frozen=True)
class BoundedExecution:
    status: str
    returncode: Optional[int]
    stdout: str
    stderr: str
    quiescent: bool
    error: Optional[str] = None
    stdout_omitted: bool = False
    stderr_omitted: bool = False


class BoundedToolExecutor:
    """Own one bounded subprocess invocation through its complete lifecycle."""

    def __init__(self, max_output_bytes: int = 256 * 1024):
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self._max_output_bytes = max_output_bytes

    def run_subprocess(
        self,
        command: Sequence[str],
        workspace: Path,
        *,
        timeout_seconds: float,
        action_id: str,
        trace=None,
        input_text: Optional[str] = None,
        omission_callback: Optional[Callable[[bool, bool], None]] = None,
    ) -> BoundedExecution:
        self._emit(trace, "tool_execution_started", action_id, {
            "command_length": len(command),
        })

        def lifecycle_event(name: str) -> None:
            event_type = {
                "timeout_requested": "tool_timeout_requested",
                "termination_started": "tool_termination_started",
                "quiescent": "tool_quiescent",
            }[name]
            self._emit(trace, event_type, action_id, {})

        try:
            outcome = OwnedProcess.run(
                command,
                cwd=workspace,
                timeout_seconds=timeout_seconds,
                input_text=input_text,
                max_output_bytes=self._max_output_bytes,
                event_callback=lifecycle_event,
            )
        except ProcessLifecycleError as error:
            result = BoundedExecution(
                status="FAILURE",
                returncode=None,
                stdout="",
                stderr="",
                quiescent=False,
                error=str(error)[:512],
            )
            self._emit(trace, "tool_execution_completed", action_id, {
                "status": result.status,
                "quiescent": False,
                "error_length": len(result.error or ""),
            })
            return result

        result = BoundedExecution(
            status="TIMEOUT" if outcome.timed_out else "SUCCESS",
            returncode=outcome.returncode,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            quiescent=outcome.quiescent,
            stdout_omitted=outcome.stdout_omitted,
            stderr_omitted=outcome.stderr_omitted,
        )
        if omission_callback is not None:
            omission_callback(result.stdout_omitted, result.stderr_omitted)
        self._emit(trace, "tool_execution_completed", action_id, {
            "status": result.status,
            "returncode": result.returncode,
            "quiescent": result.quiescent,
            "stdout_length": len(result.stdout),
            "stderr_length": len(result.stderr),
        })
        return result

    def run_ptc_worker(self, command, workspace: Path, *, timeout_seconds: float, action_id: str, initial_request: dict, request_handler, trace=None) -> BoundedExecution:
        """Run the sole PTC JSON-lines worker while retaining Runtime lifecycle ownership."""
        self._emit(trace, "tool_execution_started", action_id, {"command_length": len(command)})
        process = subprocess.Popen(list(command), cwd=workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        tree = _create_process_tree(process)
        lines, stderr = queue.Queue(), []
        def read_lines():
            for line in process.stdout:
                lines.put(line)
        def read_stderr():
            stderr.append(process.stderr.read())
        Thread(target=read_lines, daemon=True).start(); Thread(target=read_stderr, daemon=True).start()
        response = ""
        try:
            process.stdin.write(json.dumps(initial_request, ensure_ascii=False) + "\n"); process.stdin.flush()
            deadline = monotonic() + timeout_seconds
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0: raise queue.Empty
                message = json.loads(lines.get(timeout=remaining))
                if message.get("type") == "tool_call":
                    reply = request_handler(message)
                    process.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n"); process.stdin.flush()
                elif message.get("type") == "result":
                    response = json.dumps(message, ensure_ascii=False)
                    process.stdin.close(); process.wait(timeout=_PIPE_CLEANUP_SECONDS); _wait_for_quiescence(tree, 0.0)
                    self._emit(trace, "tool_quiescent", action_id, {})
                    return BoundedExecution("SUCCESS", process.returncode, response, "".join(stderr), True)
                else: raise ValueError("invalid PTC worker protocol")
        except queue.Empty:
            self._emit(trace, "tool_timeout_requested", action_id, {}); self._emit(trace, "tool_termination_started", action_id, {})
            _terminate_owned(process, tree); _wait_for_quiescence(tree, _FINAL_WAIT_SECONDS)
            self._emit(trace, "tool_quiescent", action_id, {})
            return BoundedExecution("TIMEOUT", process.returncode, "", "".join(stderr), True)
        except Exception as error:
            _terminate_owned(process, tree); _wait_for_quiescence(tree, _FINAL_WAIT_SECONDS)
            return BoundedExecution("FAILURE", process.returncode, "", "".join(stderr), True, str(error)[:512])
        finally:
            tree.close()

    @staticmethod
    def _emit(trace, event_type: str, action_id: str, data: dict) -> None:
        if trace is None:
            return
        trace.emit(event_type, {"action_id": str(action_id)[:512], **data})
