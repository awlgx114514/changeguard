from pathlib import Path
import json
import hashlib
import os
import shutil
import subprocess
import tempfile
from typing import Any, Optional

import yaml

from code_governance.contract.schema import ChangeContract
from code_governance.context.models import AgentContext as StaticAgentContext
from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.process_lifecycle import (
    _FINAL_WAIT_SECONDS,
    _PIPE_CLEANUP_SECONDS,
    _ROOT_TERMINATION_GRACE_SECONDS,
    _NoopProcessTree,
    _WindowsProcessTree,
)

from .base import AgentContext, CodingAgent, CodingAgentResult
from .provider_transport import ProviderTransportError, normalize_provider_transport, provider_transport_schema


class CodexDecisionError(RuntimeError):
    """A categorized failure from the Codex structured-decision protocol."""

    def __init__(self, category: str, detail: str = ""):
        message = category if not detail else f"{category}: {detail}"
        super().__init__(message)
        self.category = category


class CodexAgent(CodingAgent):
    """Adapter for the public ``codex exec`` command."""

    def __init__(
        self,
        executable: str = "codex",
        timeout: Optional[int] = None,
        execution_mode: str = "NATIVE",
    ):
        self._executable = executable
        self._timeout = timeout
        self._diagnostic_callback = None
        self._execution_mode = str(execution_mode).upper()
        if self._execution_mode not in {"NATIVE", "PTC"}:
            raise ValueError("execution_mode must be NATIVE or PTC")

    @property
    def decision_schema(self) -> dict:
        return provider_transport_schema(include_run_code=self._execution_mode == "PTC")

    def run(
        self,
        task: Any,
        contract: Optional[ChangeContract] = None,
        plan: Any = None,
        workspace: Optional[Path] = None,
        context: Optional[StaticAgentContext] = None,
    ) -> CodingAgentResult:
        raise RuntimeError("direct provider execution has been removed; use the canonical Action loop")

    def _build_prompt(
        self,
        task: str,
        contract: ChangeContract,
        plan: Any,
        context: Optional[StaticAgentContext] = None,
    ) -> str:
        contract_text = yaml.safe_dump(
            contract.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        )
        return (
            "User task:\n"
            f"{task.strip()}\n\n"
            "Change Contract:\n"
            f"{contract_text}\n"
            "Execution Plan:\n"
            f"{_plan_text(plan)}\n"
            "Context:\n"
            f"{_context_text(context)}\n"
            "Constraints:\n"
            "- Follow the Change Contract exactly.\n"
            "- Do not modify files outside the Contract scope.\n"
            "- Do not perform unrelated refactors.\n"
            "- Run the Contract verification commands when applicable.\n"
        )

    def decide(self, state, context) -> Action:
        """Ask Codex for one JSON Runtime Action without changing the old API."""
        workspace = (
            Path(state.workspace_reference)
            if getattr(state, "workspace_reference", "")
            else None
        )
        prompt = self._build_decision_prompt(state, context)
        with tempfile.TemporaryDirectory(prefix="changeguard-codex-") as temporary:
            schema_path = Path(temporary) / "action-schema.json"
            output_path = Path(temporary) / "last-message.json"
            schema_path.write_text(
                json.dumps(self._decision_schema_for_state(state), ensure_ascii=False),
                encoding="utf-8",
            )
            try:
                completed = self._run_cli(
                    self._decision_command(schema_path, output_path),
                    prompt=prompt,
                    workspace=workspace,
                )
            except subprocess.TimeoutExpired as error:
                raise CodexDecisionError(
                    "codex_cli_timeout", f"timeout_seconds={self._timeout}"
                ) from error
            except OSError as error:
                raise CodexDecisionError("codex_cli_launch_failed", str(error)) from error
            output_content = b""
            if output_path.is_file():
                try:
                    output_content = output_path.read_bytes()
                except OSError:
                    output_content = b""
            self._emit_diagnostic(
                "assistant_output_artifact_checked",
                artifact_present=bool(output_content),
                artifact_size_bytes=len(output_content),
                artifact_sha256=hashlib.sha256(output_content).hexdigest() if output_content else "",
            )
            if completed.returncode != 0:
                raise CodexDecisionError(
                    "codex_cli_nonzero_exit",
                    f"returncode={completed.returncode}; {_stream_diagnostics(completed)}",
                )
            self._emit_diagnostic("action_parse_started")
            action = _parse_structured_action(output_path, completed, state=state)
            self._emit_diagnostic("action_parsed", action_id=action.id, operation=action.operation.kind.value)
            return action

    def _decision_schema_for_state(self, state) -> dict:
        """Provider transport stays wide; Runtime performs deterministic validation."""
        return self.decision_schema

    def _build_decision_prompt(self, state, context) -> str:
        state_summary = {
            "task": state.task_description,
            "task_id": state.task_id,
            "current_goal": state.current_goal,
            "plan_reference": state.plan_reference,
            "context_reference": state.context_reference,
            "action_count": len(state.recent_actions),
            "observation_count": len(state.recent_observations),
            "errors": state.errors,
            "status": state.status,
        }
        context_text = _context_text(context) if isinstance(context, StaticAgentContext) else yaml.safe_dump(
            context, allow_unicode=True, sort_keys=False
        )
        operations = self._decision_schema_for_state(state)["properties"]["operation"]["enum"]
        ptc_constraints = ""
        if self._execution_mode == "PTC":
            ptc_constraints = (
                "- RUN_CODE is experimental read-only orchestration only. It may use "
                "search_files, git_status, git_diff; it cannot write/edit, use shell, "
                "network, imports, or decide FINISH.\\n"
            )
        return (
            "Decide exactly one next Agent Runtime Action.\n"
            "Return exactly one JSON object conforming to the supplied Action schema.\n"
            f"Allowed operation values: {', '.join(operations)}.\n"
            "Use arguments only for the selected operation; do not infer another operation from an invalid request.\n"
            "Use FINISH only when the task is complete. Do not include Markdown or explanation.\n\n"
            "Runtime state:\n"
            f"{yaml.safe_dump(state_summary, allow_unicode=True, sort_keys=False)}"
            "Context:\n"
            f"{context_text}"
        )

    def _decision_command(self, schema_path: Path, output_path: Path) -> list[str]:
        command = [
            *self._executable_command(),
            "exec",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-",
        ]
        return self._wrap_command(command)

    def _executable_command(self) -> list[str]:
        if os.name == "nt":
            direct_command = _windows_node_command(self._executable)
            if direct_command is not None:
                return direct_command
        return [self._executable]

    def _run_cli(self, command, *, prompt, workspace):
        kwargs = {
            "cwd": workspace,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "check": False,
        }
        if self._timeout is None:
            if prompt is not None:
                kwargs["input"] = prompt
            return subprocess.run(command, **kwargs)

        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._emit_diagnostic("process_started", pid=process.pid)
        process_tree = _create_windows_process_tree(process)
        timed_out = False
        try:
            try:
                self._emit_diagnostic("communicate_started")
                stdout, stderr = process.communicate(input=prompt, timeout=self._timeout)
            except subprocess.TimeoutExpired as error:
                timed_out = True
                self._emit_diagnostic(
                    "deadline_fired", timeout_seconds=self._timeout,
                    stdout=error.output, stderr=error.stderr,
                )
                try:
                    _terminate_after_timeout(process, process_tree, event_callback=self._emit_diagnostic)
                except Exception as cleanup_error:
                    self._emit_diagnostic(
                        "termination_failed", error_type=type(cleanup_error).__name__,
                        detail=str(cleanup_error),
                    )
                raise
            self._emit_diagnostic("process_exited", returncode=process.returncode)
            self._emit_diagnostic("stdin_completed")
            self._emit_diagnostic("pipe_eof_stdout")
            self._emit_diagnostic("pipe_eof_stderr")
            self._emit_diagnostic("communication_completed")
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            try:
                process_tree.close()
            except Exception as cleanup_error:
                if not timed_out:
                    raise
                self._emit_diagnostic(
                    "cleanup_failed", error_type=type(cleanup_error).__name__,
                    detail=str(cleanup_error),
                )
            else:
                self._emit_diagnostic("cleanup_completed")

    def _emit_diagnostic(self, boundary: str, **facts) -> None:
        callback = self._diagnostic_callback
        if callback is None:
            return
        try:
            callback(boundary, **facts)
        except Exception:
            # Diagnostic persistence must not change provider/action behavior.
            return

    @staticmethod
    def _wrap_command(command: list[str]) -> list[str]:
        if os.name == "nt" and not command[0].lower().endswith(".exe"):
            return ["cmd.exe", "/d", "/s", "/c", *command]
        return command


def _windows_node_command(executable: str) -> Optional[list[str]]:
    """Bypass npm's .CMD shim while retaining the same installed Codex package."""
    executable_path = Path(executable)
    if executable_path.suffix.lower() not in {"", ".cmd", ".bat"}:
        return None
    command_path = shutil.which(executable if executable_path.suffix else f"{executable}.cmd")
    if command_path is None:
        return None
    script_path = (
        Path(command_path).parent
        / "node_modules"
        / "@openai"
        / "codex"
        / "bin"
        / "codex.js"
    )
    node_path = shutil.which("node.exe")
    if node_path is None or not script_path.is_file():
        return None
    return [node_path, str(script_path)]


def _create_windows_process_tree(process):
    if os.name != "nt":
        return _NoopProcessTree()
    return _WindowsProcessTree(process)


def _requires_post_write_evidence(state) -> bool:
    """Whether the immediately preceding WRITE has no subsequent observation."""
    if not state.recent_actions or not state.recent_observations:
        return False
    last_action = state.recent_actions[-1]
    last_observation = state.recent_observations[-1]
    return (
        last_action.operation.kind in {OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE, OperationKind.FILE_CREATE}
        and last_observation.action_id == last_action.id
    )


def _terminate_after_timeout(process, process_tree, event_callback=None) -> None:
    def emit(boundary, **facts):
        if event_callback is not None:
            event_callback(boundary, **facts)

    emit("termination_started")
    returncode = process.poll()
    if returncode is None:
        process.terminate()
        try:
            returncode = process.wait(timeout=_ROOT_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    exit_reported = returncode is not None
    if exit_reported:
        emit("process_exited", returncode=returncode)
    process_tree.terminate()
    try:
        stdout, stderr = process.communicate(timeout=_PIPE_CLEANUP_SECONDS)
        emit("cleanup_output_observed", stdout=stdout, stderr=stderr, streams_complete=True)
        emit("pipe_eof_stdout")
        emit("pipe_eof_stderr")
        observed_returncode = getattr(process, "returncode", None)
        if observed_returncode is not None:
            returncode = observed_returncode
    except subprocess.TimeoutExpired as error:
        emit(
            "cleanup_output_observed", stdout=error.output, stderr=error.stderr,
            streams_complete=False,
        )
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                stream.close()
        returncode = process.wait(timeout=_FINAL_WAIT_SECONDS)
    if returncode is not None and not exit_reported:
        emit("process_exited", returncode=returncode)
    emit("termination_completed", returncode=returncode)


def _parse_structured_action(output_path: Path, completed, *, state=None) -> Action:
    if not output_path.exists():
        raise CodexDecisionError(
            "codex_decision_output_missing", _stream_diagnostics(completed)
        )
    try:
        content = output_path.read_text(encoding="utf-8")
    except OSError as error:
        raise CodexDecisionError("codex_decision_output_unreadable", str(error)) from error
    if not content.strip():
        raise CodexDecisionError(
            "codex_decision_output_empty", _stream_diagnostics(completed)
        )
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise CodexDecisionError("codex_decision_invalid_json", str(error)) from error
    required = ("operation", "arguments", "reason")
    if (
        not isinstance(payload, dict)
        or any(field not in payload for field in required)
        or not isinstance(payload.get("operation"), str)
        or not isinstance(payload.get("reason"), str)
    ):
        raise CodexDecisionError("codex_decision_schema_invalid")
    try:
        return normalize_provider_transport(payload, state=state)
    except ProviderTransportError as error:
        raise CodexDecisionError("codex_decision_transport_invalid", str(error)) from error


def _stream_diagnostics(completed) -> str:
    event_types = []
    errors = []
    for line in str(completed.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if isinstance(event_type, str):
            event_types.append(event_type)
        if event_type == "error" and isinstance(event.get("message"), str):
            errors.append(event["message"][:160])
    parts = [f"jsonl_events={','.join(event_types[-8:]) or 'none'}"]
    if errors:
        parts.append(f"jsonl_errors={' | '.join(errors[-2:])}")
    if completed.stderr:
        parts.append("stderr_present=true")
    return "; ".join(parts)


def _output(value: Optional[object]) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _plan_text(plan: Any) -> str:
    if plan is None:
        return "- No execution plan supplied."
    if hasattr(plan, "to_dict"):
        return yaml.safe_dump(plan.to_dict(), allow_unicode=True, sort_keys=False)
    return str(plan)


def _context_text(context: Optional[StaticAgentContext]) -> str:
    if context is None:
        return "- No static Context supplied."
    files = "\n".join(
        f"- {item.path}: {item.role}; {item.summary}"
        for item in context.relevant_files
    ) or "- None selected."
    memories = "\n".join(
        f"- [{item.id}] {item.rule} (confidence: {item.confidence})"
        for item in context.memory_context
    ) or "- None selected."
    constraints = "\n".join(f"- {item}" for item in context.constraints) or "- None."
    strategies = "\n".join(
        f"- [{item.id}] {item.strategy} (confidence: {item.confidence})"
        for item in context.strategies
    ) or "- None selected."
    tools = "\n".join(f"- {item}" for item in context.available_tools) or "- None available."
    operations = "\n".join(
        f"- {item['operation']}: {item['description']}; "
        f"when_to_use={item.get('when_to_use', ())}; "
        f"when_not_to_use={item.get('when_not_to_use', ())}; "
        f"transitions={item.get('transition_hints', ())}; "
        f"arguments={item.get('provider_arguments', ())}; "
        f"example={item.get('provider_example', {})}"
        for item in context.available_operations
    ) or "- None available."
    working_memory = "\n".join(f"- {item}" for item in context.working_memory) or "- None."
    active_issues = "\n".join(f"- {item}" for item in context.active_issues) or "- None."
    decisions = "\n".join(f"- {item}" for item in context.decisions) or "- None."
    contract = (
        yaml.safe_dump(context.contract, allow_unicode=True, sort_keys=False)
        if context.contract is not None
        else "- No validated Change Contract attached."
    )
    return (
        "Repository:\n"
        "- Root: the isolated workspace; Action paths are workspace-relative.\n"
        f"- Python modules: {len(context.repository_context.python_modules)}\n"
        "Relevant Files:\n"
        f"{files}\n"
        "Memory:\n"
        f"{memories}\n"
        "Strategies:\n"
        f"{strategies}\n"
        "Available Tools:\n"
        f"{tools}\n"
        "Available Operations:\n"
        f"{operations}\n"
        "Working Memory:\n"
        f"{working_memory}\n"
        "Active Issues:\n"
        f"{active_issues}\n"
        "Recent Decisions:\n"
        f"{decisions}\n"
        "Change Contract:\n"
        f"{contract}\n"
        "Validation Repair Evidence:\n"
        f"{yaml.safe_dump([item.to_dict() for item in context.repair_context], allow_unicode=True, sort_keys=False)}\n"
        "Context Constraints:\n"
        f"{constraints}"
    )
