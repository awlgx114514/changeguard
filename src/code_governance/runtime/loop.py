import hashlib
import json
import re
from pathlib import Path

from .action import Action, OperationKind
from .action_validator import ActionValidationError, ActionValidator
from .action_executor import CanonicalActionExecutor
from .execution import ExecutionResult, FailureRecord
from .execution import last_workspace_uncertainty_index
from .history import HistoryManager
from .observation import Observation
from .progress import ProgressEvaluator
from .state import AgentState
from .target import TargetResolutionError, TargetResolver


class AgentLoop:
    """Runs canonical Actions through governed tools until model finish or failure."""

    def __init__(
        self,
        coding_agent,
        tool_registry,
        trace_manager,
        max_steps: int = 10,
        max_consecutive_failures: int = 3,
        history_manager: HistoryManager = None,
        context_builder=None,
        action_validator: ActionValidator = None,
        target_resolver: TargetResolver = None,
        progress_evaluator: ProgressEvaluator = None,
        ptc_runtime=None,
        action_executor: CanonicalActionExecutor = None,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least one")
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least one")
        self._coding_agent = coding_agent
        self._tool_registry = tool_registry
        self._trace = trace_manager
        self._max_steps = max_steps
        self._max_consecutive_failures = max_consecutive_failures
        self._history = history_manager or HistoryManager()
        self._context_builder = context_builder
        self._action_validator = action_validator or ActionValidator()
        self._target_resolver = target_resolver or TargetResolver()
        self._action_executor = action_executor or CanonicalActionExecutor(
            tool_registry,
            action_validator=self._action_validator,
            target_resolver=self._target_resolver,
        )
        self._progress = progress_evaluator or ProgressEvaluator()
        self._ptc_runtime = ptc_runtime

    def run(self, state: AgentState, context, workspace: Path) -> AgentState:
        self._trace.emit("agent_loop_started", {"max_steps": self._max_steps, "agent": type(self._coding_agent).__name__})
        state.max_steps = self._max_steps
        consecutive_failures = 0
        while state.status not in {"STOPPED", "MODEL_FINISHED", "SUCCEEDED", "FAILED"}:
            if state.step_count >= self._max_steps:
                self._fail(state, FailureRecord(
                    stage="RUNTIME", category="MAX_STEPS", detail="agent loop reached the maximum action count"
                ))
                break
            self._transition(state, "PLANNING")
            try:
                decision_context = self._decision_context(context, state)
                self._trace.emit("decision_context_prepared", self._decision_context_diagnostics(decision_context))
                raw_action = self._coding_agent.decide(state, decision_context)
                if not isinstance(raw_action, Action):
                    raise TypeError("CodingAgent.decide must return Action")
            except Exception as error:
                self._fail(state, self._failure("ADAPTER", "DECISION_ERROR", error))
                break

            try:
                action = self._action_executor.prepare(raw_action, workspace)
            except Exception as error:
                self._trace_action(raw_action, self._action_target(raw_action), None)
                state.record_action(raw_action)
                result = self._failure_result(raw_action, error)
                self._record_result(state, raw_action, result)
                progress = self._progress.evaluate(raw_action, result, state)
                self._trace.emit(
                    "progress_evaluated",
                    {"action_id": raw_action.id, "outcome": progress.outcome, "evidence": progress.evidence},
                )
                if progress.outcome == "NO_PROGRESS":
                    state.termination_reason = "NO_PROGRESS"
                    self._transition(state, "STOPPED")
                    continue
                if self._is_recoverable_governance_failure(result.failure):
                    consecutive_failures += 1
                    if consecutive_failures >= self._max_consecutive_failures:
                        self._fail(state, FailureRecord(
                            stage="RUNTIME",
                            category="CONSECUTIVE_FAILURES",
                            action_id=raw_action.id,
                            detail="agent loop reached consecutive action failures",
                        ))
                    continue
                self._fail(state, result.failure)
                break

            self._trace_action(action, self._action_target(raw_action), self._action_target(action))
            state.record_action(action)
            self._transition(state, "EXECUTING")
            result = (
                self._write_safety_result(action, state, workspace)
                or self._repeated_read_safety_result(action, state)
                or self._execute(action, workspace)
            )
            progress = self._progress.evaluate(action, result, state)
            self._record_result(state, action, result)
            self._trace.emit(
                "progress_evaluated",
                {"action_id": action.id, "outcome": progress.outcome, "evidence": progress.evidence},
            )
            if action.operation.kind is OperationKind.WORKFLOW_FINISH and result.success:
                if not state.termination_reason:
                    state.termination_reason = "MODEL_FINISH"
                self._transition(state, "MODEL_FINISHED")
            elif progress.outcome == "FAILURE":
                consecutive_failures += 1
                if result.failure is not None and not self._is_recoverable_governance_failure(result.failure):
                    state.set_failure(result.failure)
                if consecutive_failures >= self._max_consecutive_failures:
                    self._fail(state, FailureRecord(
                        stage="RUNTIME",
                        category="CONSECUTIVE_FAILURES",
                        action_id=action.id,
                        detail="agent loop reached consecutive action failures",
                    ))
            elif progress.outcome == "NO_PROGRESS":
                if progress.evidence in {
                    "identical_action_and_result",
                    "deterministic_rejection",
                    "repeated_successful_verification",
                }:
                    state.termination_reason = "NO_PROGRESS"
                    self._transition(state, "STOPPED")
            else:
                consecutive_failures = 0
        self._trace.emit(
            "agent_loop_finished",
            {"status": state.status, "actions": len(state.recent_actions), "errors": len(state.errors)},
        )
        return state

    @staticmethod
    def _is_recoverable_governance_failure(failure: FailureRecord) -> bool:
        return failure.error_type in {"PreExecutionGovernanceError", "ActionValidationError"}

    def _write_safety_result(self, action: Action, state: AgentState, workspace: Path):
        if action.operation.kind not in {OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE, OperationKind.FILE_CREATE}:
            return None

        if action.operation.kind is OperationKind.FILE_CREATE:
            return None

        if action.operation.kind is OperationKind.FILE_REPLACE:
            return self._replace_safety_result(action, state, workspace)

        history = list(zip(state.recent_actions[:-1], state.recent_observations))
        writes = [
            index
            for index, (previous_action, observation) in enumerate(history)
            if (
                previous_action.operation.kind in {OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE, OperationKind.FILE_CREATE}
                and previous_action.operation.fields.get("path") == action.operation.fields["path"]
                and observation.success
            )
        ]
        last_write = writes[-1] if writes else -1
        if last_write >= 0 and not any(
            observation.success and observation.metadata.get("information_gain")
            for _, observation in history[last_write + 1 :]
        ):
            return self._pre_execution_failure(
                action,
                "REPEATED_WRITE_WITHOUT_NEW_EVIDENCE",
                "a successful write requires new read or verification evidence before another write to the same target",
            )

        path = (Path(workspace).resolve() / action.operation.fields["path"]).resolve()
        if not path.is_file():
            return None
        try:
            current_fingerprint = self._hash(path.read_text(encoding="utf-8"))
        except OSError as error:
            return self._pre_execution_failure(action, "WRITE_PROVENANCE_UNAVAILABLE", str(error))

        provenance = history[last_write + 1 :] if last_write >= 0 else history
        has_complete_read = any(
            previous_action.operation.kind is OperationKind.FILE_READ
            and previous_action.operation.fields.get("path") == action.operation.fields["path"]
            and observation.success
            and observation.metadata.get("content_truncated") is False
            and observation.metadata.get("file_hash") == current_fingerprint
            for previous_action, observation in provenance
        )
        has_current_write_provenance = (
            last_write >= 0
            and history[last_write][1].metadata.get("content_fingerprint") == current_fingerprint
        )
        if has_complete_read or has_current_write_provenance:
            return None
        return self._pre_execution_failure(
            action,
            "FULL_FILE_WRITE_REQUIRES_COMPLETE_READ",
            "a full-file write requires a complete current read of the target",
        )

    def _replace_safety_result(self, action: Action, state: AgentState, workspace: Path):
        path = (Path(workspace).resolve() / action.operation.fields["path"]).resolve()
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as error:
            return self._pre_execution_failure(action, "REPLACE_PROVENANCE_UNAVAILABLE", str(error))
        expected = action.operation.fields["expected_text"]
        observed_hash = action.operation.fields["observed_file_hash"]
        if self._hash(current) != observed_hash:
            return self._pre_execution_failure(action, "REPLACE_PROVENANCE_STALE", "file.replace observed_file_hash is stale")
        observed = any(
            previous.operation.kind is OperationKind.FILE_READ
            and previous.operation.fields.get("path") == action.operation.fields["path"]
            and observation.success
            and observation.metadata.get("file_hash") == observed_hash
            and expected in str(observation.metadata.get("content_preview", ""))
            for previous, observation in zip(state.recent_actions[:-1], state.recent_observations)
        )
        if not observed:
            return self._pre_execution_failure(
                action,
                "LOCAL_REPLACE_REQUIRES_OBSERVED_TEXT",
                "file.replace expected_text must come from a current read observation",
            )
        return None

    def _repeated_read_safety_result(self, action: Action, state: AgentState):
        if action.operation.kind is not OperationKind.FILE_READ:
            return None
        history = list(zip(state.recent_actions[:-1], state.recent_observations))
        history = history[last_workspace_uncertainty_index(history) + 1 :]
        repeated_reads = [
            index
            for index, (previous_action, observation) in enumerate(history)
            if (
                previous_action.operation.kind is OperationKind.FILE_READ
                and previous_action.operation.fields == action.operation.fields
                and observation.success
            )
        ]
        if not repeated_reads:
            return None
        last_read = repeated_reads[-1]
        has_new_evidence = any(
            observation.success
            and (observation.metadata.get("information_gain") or observation.metadata.get("state_changed"))
            for _, observation in history[last_read + 1 :]
        )
        if has_new_evidence:
            return None
        return self._pre_execution_failure(
            action,
            "REPEATED_READ_WITHOUT_NEW_EVIDENCE",
            "a successful read requires a different action or new evidence before repeating the same read",
        )

    def _pre_execution_failure(self, action: Action, category: str, detail: str) -> ExecutionResult:
        failure = FailureRecord(
            stage="PRE_EXECUTION_GOVERNANCE",
            category=category,
            action_id=action.id,
            detail=detail,
            error_type="PreExecutionGovernanceError",
            error_hash=self._hash(detail),
        )
        return ExecutionResult(
            action_id=action.id,
            status="FAILURE",
            canonical_target=self._action_target(action),
            result_fingerprint=failure.error_hash,
            summary=detail,
            metadata={
                "failure_stage": failure.stage,
                "failure_category": failure.category,
                "effect_uncertain": False,
                "output_partial": False,
            },
            failure=failure,
        )

    def _execute(self, action: Action, workspace: Path) -> ExecutionResult:
        if action.operation.kind is OperationKind.PTC_RUN:
            if self._ptc_runtime is None:
                return self._failure_result(action, ValueError("PTC runtime is unavailable"))
            return self._ptc_runtime.execute(action, workspace)
        if action.operation.kind in {
            OperationKind.FILE_READ, OperationKind.FILE_FIND, OperationKind.FILE_SEARCH,
            OperationKind.FILE_REPLACE, OperationKind.FILE_WRITE, OperationKind.FILE_CREATE,
            OperationKind.COMMAND_RUN, OperationKind.GIT_STATUS, OperationKind.GIT_DIFF,
        }:
            return self._action_executor.execute_prepared(action, workspace)
        if action.operation.kind is OperationKind.CONTEXT_CURRENT:
            return ExecutionResult(
                action_id=action.id,
                status="SUCCESS",
                effect_type="CONTEXT",
                information_gain=True,
                result_fingerprint="current_context",
                summary="Current context remains available",
                metadata={"context_reference": "current_context"},
            )
        if action.operation.kind is OperationKind.WORKFLOW_FINISH:
            return ExecutionResult(
                action_id=action.id,
                status="SUCCESS",
                effect_type="FINISH",
                result_fingerprint="model_finished",
                summary="Model requested verification",
            )
        return self._failure_result(action, ValueError(f"unsupported operation: {action.operation.kind.value}"))

    def _record_result(self, state, action, result: ExecutionResult) -> None:
        observation = Observation.from_execution(result, self._observation_type(action))
        self._trace.emit(
            "action_executed",
            {
                "action_id": action.id,
                "operation": action.operation.kind.value,
                "target": self._action_target(action),
                "tool": self._tool_name(action),
                "success": result.success,
            },
        )
        state.record_observation(observation)
        history = self._history.record(state, action, observation)
        self._trace.emit("history_compressed", history)
        data = {
            "action_id": observation.action_id,
            "success": observation.success,
            "observation_type": observation.type,
            "result_type": observation.result_type,
            "error_category": observation.error_category,
            "summary_length": len(observation.summary),
            "summary_hash": self._hash(observation.summary),
            "artifact_reference": observation.artifact_reference,
            "metadata_keys": sorted(observation.metadata),
            "failure_stage": None if result.failure is None else result.failure.stage,
            "result_fingerprint": result.result_fingerprint,
        }
        self._trace.emit("observation_received", data)

    def _failure_result(self, action: Action, error: Exception) -> ExecutionResult:
        return self._action_executor.failure_result(action, error)

    def _fail(self, state: AgentState, failure: FailureRecord) -> None:
        state.set_failure(failure)
        state.errors.append(failure.detail or failure.category)
        self._transition(state, "FAILED")

    def _transition(self, state: AgentState, status: str) -> None:
        state.transition(status)
        self._trace.emit(
            "state_updated",
            {
                "status": state.status,
                "actions": len(state.recent_actions),
                "observations": len(state.recent_observations),
                "errors": len(state.errors),
                "working_items": len(state.working_memory_summary),
                "open_issues": len(state.open_issues),
            },
        )

    def _trace_action(self, action, raw_target: str, canonical_target: str) -> None:
        self._trace.emit(
            "action_decided",
            {
                "action_id": action.id,
                "operation": action.operation.kind.value,
                "target": canonical_target or "",
                "raw_target": raw_target[:512],
                "canonical_target": canonical_target,
                "agent": type(self._coding_agent).__name__,
            },
        )

    def _tool_name(self, action: Action) -> str:
        if self._tool_registry is None:
            return ""
        try:
            return self._tool_registry.tool_request(action)[0]
        except ValueError:
            return ""

    @staticmethod
    def _observation_type(action: Action) -> str:
        return {
            OperationKind.FILE_READ: "file_content",
            OperationKind.FILE_FIND: "file_discovery",
            OperationKind.FILE_SEARCH: "file_search",
            OperationKind.FILE_REPLACE: "file_written",
            OperationKind.FILE_WRITE: "file_written",
            OperationKind.FILE_CREATE: "file_written",
            OperationKind.COMMAND_RUN: "command_result",
            OperationKind.GIT_STATUS: "git_result",
            OperationKind.GIT_DIFF: "git_result",
            OperationKind.PTC_RUN: "ptc_result",
            OperationKind.CONTEXT_CURRENT: "context_status",
            OperationKind.WORKFLOW_FINISH: "finish_result",
        }[action.operation.kind]

    @staticmethod
    def _action_target(action: Action) -> str:
        fields = action.operation.fields
        for field in ("path", "directory", "name_query", "query"):
            value = fields.get(field)
            if isinstance(value, str):
                return value
        return ""

    def _decision_context(self, context, state: AgentState):
        if self._context_builder is not None:
            return self._context_builder.with_state(context, state)
        return context

    def _decision_context_diagnostics(self, context) -> dict:
        payload = context.to_dict() if hasattr(context, "to_dict") else context
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        working_memory = tuple(getattr(context, "working_memory", ()))
        active_issues = tuple(getattr(context, "active_issues", ()))
        return {
            "relevant_files": [item.path for item in getattr(context, "relevant_files", ())],
            "working_memory_count": len(working_memory),
            "working_memory_lengths": [len(item) for item in working_memory],
            "working_memory_hashes": [self._hash(item) for item in working_memory],
            "working_memory_content_hashes": sorted({
                match.group(1)
                for item in working_memory
                for match in re.finditer(r"content_fingerprint=([0-9a-f]{64})", item)
            }),
            "active_issue_count": len(active_issues),
            "active_issue_hashes": [self._hash(item) for item in active_issues],
            "section_lengths": {
                "relevant_files": len(getattr(context, "relevant_files", ())),
                "working_memory": sum(len(item) for item in working_memory),
                "active_issues": sum(len(item) for item in active_issues),
                "decisions": sum(len(item) for item in getattr(context, "decisions", ())),
            },
            "context_fingerprint": self._hash(encoded),
        }

    @staticmethod
    def _failure(stage: str, category: str, error: Exception = None, action_id: str = "") -> FailureRecord:
        detail = "" if error is None else str(error)[:512]
        return FailureRecord(
            stage=stage,
            category=category,
            action_id=action_id,
            detail=detail,
            error_type="" if error is None else type(error).__name__,
            error_hash=AgentLoop._hash(detail) if detail else "",
        )

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
