from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol, Tuple
import json
import uuid
import hashlib
import shutil

import yaml

from code_governance.contract.schema import ChangeContract
from code_governance.contract.validator import ContractValidator
from code_governance.git.context import GitContextError
from code_governance.memory.manager import MemoryManager
from code_governance.memory.models import FailureRecord
from code_governance.memory.retriever import MemoryRetriever
from code_governance.repo.analyzer import RepoAnalysisError, RepoContext, RepositoryAnalyzer
from code_governance.report.report import ChangeReport, build_report, report_to_dict
from code_governance.validator.core import ValidationResult, ValidatorEngine
from code_governance.validator.assertions import AssertionEvaluatorRegistry
from code_governance.validator.repair_context import RepairFailureContext, build_repair_contexts
from code_governance.validator.evidence import (
    ValidationEvidence,
    ValidationEvidenceError,
    collect_workspace_snapshot,
)
from code_governance.verification.runner import execute_verification_claims
from code_governance.workspace.manager import WorkspaceError, WorkspaceManager
from code_governance.context.builder import ContextBuilder
from code_governance.context.models import AgentContext as StaticAgentContext
from code_governance.tools import (
    ApprovalProvider,
    FilesystemTool,
    GitTool,
    HardGuard,
    ShellTool,
    ToolPolicy,
    ToolRegistry,
)
from code_governance.trace.writer import TraceManager, TraceWriter
from code_governance.runtime.loop import AgentLoop
from code_governance.runtime.state import AgentState
from code_governance.runtime.action_validator import ActionValidator
from code_governance.runtime.action_executor import CanonicalActionExecutor
from code_governance.runtime.history import HistoryManager, Issue
from code_governance.runtime.execution import FailureRecord as RuntimeFailureRecord
from code_governance.runtime.ptc import PTCCodeRuntime

from .base import CodingAgent, CodingAgentResult
from .contract_agent import ContractAgent, ContractGenerationError
from .planner import Plan, PlannerAgent
from .repair import RepairAgent, RepairPlan


@dataclass(frozen=True)
class ValidationAttempt:
    """Retain original immutable facts; attempt identity scopes repeated request ids."""

    attempt_id: str
    evidence: ValidationEvidence
    result: ValidationResult
    failure_contexts: Tuple[RepairFailureContext, ...] = ()
    repair_failure_ids: Tuple[str, ...] = ()
    action_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessRunResult:
    contract_path: Optional[Path]
    contract: Optional[ChangeContract]
    repo_context: Optional[RepoContext]
    plan: Optional[Plan]
    report: Optional[ChangeReport]
    executions: Tuple[CodingAgentResult, ...]
    repairs: int
    patch_path: Optional[Path]
    validation_evidence: Optional[ValidationEvidence] = None
    validator_result: Optional[ValidationResult] = None
    error: Optional[str] = None
    failure: Optional[RuntimeFailureRecord] = None
    termination_reason: str = ""
    validation_result: str = "NOT_RUN"
    final_outcome: str = "FAILED"
    validation_attempts: Tuple[ValidationAttempt, ...] = ()

    @property
    def success(self) -> bool:
        return (
            self.error is None
            and self.validation_evidence is not None
            and self.validator_result is not None
            and self.validator_result.status == "PASS"
        )


@dataclass(frozen=True)
class RepairStartResult:
    """Outcome plus the independently exportable state of one restricted fork."""

    harness_result: HarnessRunResult
    runtime_state: AgentState
    checkpoint_id: str
    checkpoint_validation_attempt_id: str
    run_id: str
    workspace_id: str
    starting_workspace_fingerprint: str
    final_workspace_path: Path
    final_workspace_fingerprint: str


@dataclass(frozen=True)
class _WorkspaceValidation:
    """Facts and deterministic decision from one validation attempt."""

    evidence: ValidationEvidence
    result: ValidationResult
    report: ChangeReport


class HarnessObserver(Protocol):
    def on_context(self, context: StaticAgentContext) -> None:
        raise NotImplementedError

    def on_runtime_state(self, state: AgentState) -> None:
        raise NotImplementedError


class _CheckpointRepairDecisionAgent:
    """Apply the checkpoint RepairContext intervention to one decision only."""

    def __init__(self, delegate, frozen_repair_context, observe_state):
        self._delegate = delegate
        self._observe_state = observe_state
        self._decision_count = 0
        self._frozen_context = {
            self._identity(item) for item in frozen_repair_context
        }

    @staticmethod
    def _identity(item):
        value = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def decide(self, state, context):
        try:
            self._observe_state(state)
        except Exception:
            # Evaluation persistence is observational; it cannot replace a decision.
            pass
        if self._decision_count and self._frozen_context and isinstance(context, StaticAgentContext):
            remaining = tuple(
                item for item in context.repair_context
                if self._identity(item) not in self._frozen_context
            )
            if len(remaining) != len(context.repair_context):
                context = replace(context, repair_context=remaining)
        self._decision_count += 1
        return self._delegate.decide(state, context)


class HarnessOrchestrator:
    """Coordinates the v2 isolated Coding Agent Harness flow."""

    def __init__(
        self,
        contract_agent: Optional[ContractAgent] = None,
        coding_agent: Optional[CodingAgent] = None,
        validator: Optional[ValidatorEngine] = None,
        contract_validator: Optional[ContractValidator] = None,
        repository_analyzer: Optional[RepositoryAnalyzer] = None,
        planner: Optional[PlannerAgent] = None,
        workspace_manager: Optional[WorkspaceManager] = None,
        repair_agent: Optional[RepairAgent] = None,
        context_builder: Optional[ContextBuilder] = None,
        tool_registry: Optional[ToolRegistry] = None,
        approval_provider: Optional[ApprovalProvider] = None,
        hard_guards: Optional[Tuple[HardGuard, ...]] = None,
        project_rules: Tuple[str, ...] = (),
        max_repairs: int = 2,
        runtime_loop: bool = True,
        execution_mode: str = "NATIVE",
        runtime_max_steps: int = 10,
        history_manager_factory: Optional[Callable[[], HistoryManager]] = None,
        trace_manager_factory: Optional[Callable[[str, TraceWriter], TraceManager]] = None,
        action_validator_factory: Optional[Callable[[], ActionValidator]] = None,
        runtime_repair: bool = True,
        runtime_issue_lifecycle: bool = True,
        observer: Optional[HarnessObserver] = None,
    ):
        if max_repairs < 0:
            raise ValueError("max_repairs must not be negative")
        if runtime_max_steps < 1:
            raise ValueError("runtime_max_steps must be at least one")
        execution_mode = str(execution_mode).upper()
        if execution_mode not in {"NATIVE", "PTC"}:
            raise ValueError("execution_mode must be NATIVE or PTC")
        if not runtime_loop:
            raise ValueError("direct execution has been removed; Harness always uses the canonical Action loop")
        if coding_agent is None:
            from .codex_agent import CodexAgent

            coding_agent = CodexAgent(execution_mode=execution_mode)
        self._contract_agent = contract_agent or ContractAgent()
        self._coding_agent = coding_agent
        self._validator = validator or ValidatorEngine()
        self._contract_validator = contract_validator or ContractValidator()
        self._repository_analyzer = repository_analyzer or RepositoryAnalyzer()
        self._planner = planner or PlannerAgent()
        self._workspace_manager = workspace_manager or WorkspaceManager()
        self._repair_agent = repair_agent or RepairAgent()
        self._context_builder = context_builder or ContextBuilder()
        self._tool_registry = (
            tool_registry
            if tool_registry is not None
            else self._default_tool_registry(approval_provider, hard_guards)
        )
        self._project_rules = project_rules
        self._max_repairs = max_repairs
        self._runtime_loop = True
        self._execution_mode = execution_mode
        self._runtime_max_steps = runtime_max_steps
        self._history_manager_factory = history_manager_factory
        self._trace_manager_factory = trace_manager_factory
        self._action_validator_factory = action_validator_factory
        self._runtime_repair = runtime_repair
        self._runtime_issue_lifecycle = runtime_issue_lifecycle
        self._observer = observer

    def run(
        self,
        task_file: Path,
        repo_path: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> HarnessRunResult:
        try:
            task = Path(task_file).read_text(encoding="utf-8")
        except OSError as error:
            return self._failure(f"unable to read task file: {error}")

        self._progress(progress, 1, "Analyze repository")
        try:
            repo_context = self._repository_analyzer.analyze(repo_path)
        except RepoAnalysisError as error:
            return self._failure(f"unable to analyze repository: {error}")
        memory = MemoryManager(repo_context.root)
        run_id = uuid.uuid4().hex
        trace_writer = TraceWriter(repo_context.root)
        trace = (
            self._trace_manager_factory(run_id, trace_writer)
            if self._trace_manager_factory is not None
            else TraceManager(run_id, trace_writer)
        )
        self._tool_registry.set_trace_manager(trace)
        trace.emit("task_started", {"task_file": str(task_file)})
        context = self._context_builder.build(
            task,
            repo_context,
            MemoryRetriever(memory),
            self._project_rules,
            tool_registry=self._tool_registry,
        )
        trace.set_context_version(context.version)
        trace.emit(
            "context_created",
            {
                "version": context.version,
                "files": [item.path for item in context.relevant_files],
                "memories": [item.id for item in context.memory_context],
                "strategies": [item.id for item in context.strategies],
                "tools": list(context.available_tools),
            },
        )
        trace.emit(
            "memory_retrieved",
            {
                "constraints": [item.id for item in context.memory_context],
                "strategies": [item.id for item in context.strategies],
            },
        )

        self._progress(progress, 2, "Generate contract")
        try:
            contract = self._contract_agent.generate(context)
            contract_result = self._contract_validator.validate(contract)
            trace.emit(
                "contract_validated",
                {
                    "valid": contract_result.is_valid,
                    "findings": [finding.code for finding in contract_result.findings],
                },
            )
            if not contract_result.is_valid:
                error = "; ".join(finding.message for finding in contract_result.findings if finding.severity == "error")
                trace.emit("task_finished", {"status": "ERROR", "error": error})
                trace.write()
                return self._failure(
                    f"invalid Change Contract: {error}",
                    contract=contract,
                    repo_context=repo_context,
                )
            contract_path = self._write_contract(task_file, contract)
        except (ContractGenerationError, OSError, yaml.YAMLError) as error:
            trace.emit("task_finished", {"status": "ERROR", "error": str(error)})
            trace.write()
            return self._failure(
                f"unable to generate Contract: {error}", repo_context=repo_context
            )
        trace.emit("decision_made", {"decision": "contract_generated", "contract_path": str(contract_path)})
        context = self._context_builder.with_contract(context, contract)
        if self._observer is not None:
            self._observer.on_context(context)
        trace.emit("context_contract_attached", {"version": contract.version})

        self._progress(progress, 3, "Generate plan")
        plan = self._planner.plan(context, contract)
        trace.emit(
            "plan_created",
            {
                "steps": len(plan.steps),
                "constraints": len(plan.context_constraints),
                "strategies": len(plan.strategy_guidance),
            },
        )

        self._progress(progress, 4, "Create workspace")
        try:
            workspace = self._workspace_manager.create(repo_context.root)
        except WorkspaceError as error:
            trace.emit("task_finished", {"status": "ERROR", "error": str(error)})
            trace.write()
            return self._failure(
                f"unable to create workspace: {error}",
                contract_path=contract_path,
                contract=contract,
                repo_context=repo_context,
                plan=plan,
            )

        executions = []
        repairs = 0
        report = None
        validation_run = None
        validation_attempts = []
        runtime_state = None
        repair_issue_ids = []
        try:
            self._progress(progress, 5, "Execute coding agent")
            runtime_state = AgentState.initialize(
                task,
                workspace=workspace.path,
                run_id=run_id,
                max_steps=self._runtime_max_steps,
                task_id=run_id,
                workspace_reference=str(workspace.path),
                plan_reference=f"plan:{run_id}",
                context_reference=f"context:{context.version}",
                contract=contract,
            )
            runtime_state = self._run_runtime_loop(runtime_state, context, workspace.path, trace)
            self._observe_runtime_state(runtime_state)
            if not self._is_validation_eligible(runtime_state):
                error = runtime_state.errors[-1] if runtime_state.errors else "runtime loop did not finish"
                trace.emit("task_finished", {"status": "ERROR", "error": error})
                trace.write()
                return self._failure(
                    f"runtime loop failed: {error}",
                    contract_path=contract_path,
                    contract=contract,
                    repo_context=repo_context,
                    plan=plan,
                    runtime_failure=runtime_state.failure,
                    termination_reason=runtime_state.termination_reason,
                )
            execution = CodingAgentResult(
                stdout="runtime loop completed",
                stderr="",
                returncode=0,
            )
            executions.append(execution)
            if not execution.success:
                return self._agent_failure(
                    execution, contract_path, contract, repo_context, plan, report,
                    executions, repairs, memory, run_id, task, context, trace,
                )

            self._progress(progress, 6, "Validate workspace")
            if runtime_state is not None:
                self._transition_runtime_state(runtime_state, "VERIFYING", trace)
                self._observe_runtime_state(runtime_state)
            validation_run = self._validate(contract, workspace.path)
            if isinstance(validation_run, str):
                trace.emit("task_finished", {"status": "ERROR", "error": validation_run})
                trace.write()
                return self._failure(
                    validation_run,
                    contract_path=contract_path,
                    contract=contract,
                    repo_context=repo_context,
                    plan=plan,
                    executions=tuple(executions),
                )
            report = validation_run.report
            validation_attempts.append(self._validation_attempt(
                validation_run, contract, run_id, 1,
                action_ids=tuple(action.id for action in runtime_state.recent_actions),
            ))
            self._observe_validation_attempt(validation_attempts[-1])

            while validation_run.result.status == "FAIL":
                trace.emit(
                    "validation_failed",
                    {
                        "findings": [finding.code for finding in validation_run.result.findings],
                        "files": list(validation_run.evidence.change_context.changed_files),
                        "attempt_id": validation_attempts[-1].attempt_id,
                        "failure_ids": [item.failure_id for item in validation_attempts[-1].failure_contexts],
                    },
                )
                memory.record_failure(
                    self._failure_record(run_id, task, contract, validation_run)
                )
                if repairs >= self._max_repairs:
                    break
                # Process/environment and invalid-evidence failures are not coding
                # repairs. Keep existing governance-finding recovery unchanged.
                if (any(item.execution_failed for item in validation_run.evidence.verification)
                        or not validation_run.evidence.workspace_is_consistent
                        or any(finding.code in {"ASSERTION_UNSUPPORTED", "expected_change_evidence_missing"}
                               for finding in validation_run.result.findings)):
                    break
                self._progress(progress, 7, "Repair validator failures")
                repair_contexts = validation_attempts[-1].failure_contexts
                context = self._context_builder.with_repair_context(context, repair_contexts)
                if self._observer is not None:
                    self._observer.on_context(context)
                trace.emit("repair_started", {"attempt": repairs + 1,
                                              "failure_ids": [item.failure_id for item in repair_contexts]})
                preceding_actions = {action.id for action in runtime_state.recent_actions}
                if self._runtime_repair:
                    repair_issue_ids = []
                    if self._runtime_issue_lifecycle:
                        runtime_state.retry_count += 1
                        runtime_state.plan_reference = f"repair:{repairs + 1}"
                        runtime_state.current_goal = "Repair validator failures"
                        for finding in validation_run.result.findings:
                            description = self._repair_issue_description(
                                finding, validation_run.evidence
                            )
                            issue = Issue.create(
                                "validation_failure",
                                description,
                            )
                            runtime_state.issues.append(issue)
                            repair_issue_ids.append(issue.id)
                    self._transition_runtime_state(runtime_state, "REPAIRING", trace)
                    runtime_state.termination_reason = ""
                    runtime_state = self._run_runtime_loop(runtime_state, context, workspace.path, trace)
                    self._observe_runtime_state(runtime_state)
                    execution = CodingAgentResult(
                        stdout="runtime repair completed",
                        stderr="",
                        returncode=0 if runtime_state.status == "MODEL_FINISHED" else None,
                    )
                else:
                    break
                executions.append(execution)
                repairs += 1
                if not execution.success:
                    return self._agent_failure(
                        execution, contract_path, contract, repo_context, plan, report,
                        executions, repairs, memory, run_id, task, context, trace,
                        validation_attempts=tuple(validation_attempts),
                        runtime_state=runtime_state,
                    )
                if runtime_state is not None and self._runtime_issue_lifecycle:
                    self._transition_runtime_state(runtime_state, "VERIFYING", trace)
                    self._observe_runtime_state(runtime_state)
                validation_run = self._validate(contract, workspace.path)
                if isinstance(validation_run, str):
                    trace.emit("task_finished", {"status": "ERROR", "error": validation_run})
                    trace.write()
                    return self._failure(
                        validation_run,
                        contract_path=contract_path,
                        contract=contract,
                        repo_context=repo_context,
                        plan=plan,
                        executions=tuple(executions),
                        repairs=repairs,
                        validation_attempts=tuple(validation_attempts),
                    )
                report = validation_run.report
                validation_attempts.append(self._validation_attempt(
                    validation_run, contract, run_id, len(validation_attempts) + 1,
                    repair_failure_ids=tuple(item.failure_id for item in repair_contexts),
                    action_ids=tuple(action.id for action in runtime_state.recent_actions if action.id not in preceding_actions),
                ))
                self._observe_validation_attempt(validation_attempts[-1])
                if (
                    runtime_state is not None
                    and self._runtime_issue_lifecycle
                    and validation_run.result.status != "FAIL"
                ):
                    for issue_id in repair_issue_ids:
                        runtime_state.resolve_issue(issue_id)

            if runtime_state is not None:
                runtime_state.validation_result = validation_run.result.status
                runtime_state.final_outcome = {
                    "PASS": "SUCCEEDED",
                    "WARNING": "COMPLETED_WITH_WARNINGS",
                    "FAIL": "FAILED",
                }[validation_run.result.status]
                if validation_run.result.status == "PASS":
                    self._transition_runtime_state(runtime_state, "SUCCEEDED", trace)
                elif validation_run.result.status == "WARNING":
                    self._transition_runtime_state(runtime_state, "FINISHED", trace)
                else:
                    runtime_state.set_failure(RuntimeFailureRecord(
                        stage="VALIDATION",
                        category="VALIDATOR_FAILED",
                        detail="validator rejected the resulting workspace",
                    ))
                    self._transition_runtime_state(runtime_state, "FAILED", trace)
                self._observe_runtime_state(runtime_state)

            patch_path = self._workspace_manager.save_patch(workspace)
            self._record(
                memory,
                run_id,
                task,
                repo_context,
                contract,
                plan,
                executions,
                validation_run,
                repairs,
                patch_path,
                context,
                trace,
            )
            self._progress(progress, 8, "Report")
            return HarnessRunResult(
                contract_path=contract_path,
                contract=contract,
                repo_context=repo_context,
                plan=plan,
                report=report,
                executions=tuple(executions),
                repairs=repairs,
                patch_path=patch_path,
                validation_evidence=validation_run.evidence,
                validator_result=validation_run.result,
                failure=None if runtime_state is None else runtime_state.failure,
                termination_reason="" if runtime_state is None else runtime_state.termination_reason,
                validation_result=validation_run.result.status,
                final_outcome={
                    "PASS": "SUCCEEDED",
                    "WARNING": "COMPLETED_WITH_WARNINGS",
                    "FAIL": "FAILED",
                }[validation_run.result.status],
                validation_attempts=tuple(validation_attempts),
            )
        except WorkspaceError as error:
            return self._failure(
                f"workspace failure: {error}",
                contract_path=contract_path,
                contract=contract,
                repo_context=repo_context,
                plan=plan,
                report=report,
                executions=tuple(executions),
                repairs=repairs,
                termination_reason=("" if runtime_state is None else runtime_state.termination_reason),
                validation_result=("NOT_RUN" if runtime_state is None else runtime_state.validation_result),
                validation_attempts=tuple(validation_attempts),
            )
        finally:
            self._workspace_manager.cleanup(workspace)

    def repair_start(
        self,
        checkpoint,
        *,
        repo_path: Path,
        trial_id: str,
        run_id: str,
        include_repair_context: bool,
        final_workspace_path: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> RepairStartResult:
        """Fork only a validated failed-validation checkpoint into the normal repair loop.

        This is deliberately narrower than Runtime resume: checkpoints must be
        produced by the evaluation boundary, preserve a real failed Validation,
        and carry the frozen total budgets and recoverable workspace snapshot.
        """
        checkpoint.validate()
        payload = checkpoint.payload
        budgets = payload["budgets"]
        if budgets["steps_total"] != self._runtime_max_steps:
            raise ValueError("repair_start runtime step budget differs from the frozen checkpoint budget")
        if budgets["repairs_total"] != self._max_repairs:
            raise ValueError("repair_start repair budget differs from the frozen checkpoint budget")
        if not trial_id or not run_id:
            raise ValueError("repair_start requires trial_id and run_id")

        repository = Path(repo_path).resolve()
        checkpoint.validate_baseline(repository)
        task = str(payload["task"]["description"])
        contract = ChangeContract.model_validate(payload["contract"])
        contract_result = self._contract_validator.validate(contract)
        if not contract_result.is_valid:
            errors = tuple(
                finding for finding in contract_result.findings
                if finding.severity == "error"
            )
            codes = ", ".join(finding.code for finding in errors)
            details = "; ".join(finding.message for finding in errors)
            raise ValueError(
                "checkpoint Change Contract failed current semantic admission "
                f"[{codes}]: {details}"
            )
        plan_data = payload["plan"]
        plan = Plan(
            steps=tuple(plan_data["steps"]),
            memory_constraints=tuple(plan_data.get("memory_constraints", ())),
            context_constraints=tuple(plan_data.get("context_constraints", ())),
            strategy_guidance=tuple(plan_data.get("strategy_guidance", ())),
        )
        context = checkpoint.restore_context(repository)
        failures = checkpoint.restore_repair_context()
        context = self._context_builder.with_repair_context(
            context, failures if include_repair_context else ()
        )
        if self._observer is not None:
            self._observer.on_context(context)

        memory = MemoryManager(repository)
        trace = (
            self._trace_manager_factory(run_id, TraceWriter(repository))
            if self._trace_manager_factory is not None
            else TraceManager(run_id, TraceWriter(repository))
        )
        self._tool_registry.set_trace_manager(trace)
        trace.set_context_version(context.version)
        trace.emit("task_started", {
            "task_file": "<checkpoint>",
            "checkpoint_id": checkpoint.checkpoint_id,
            "trial_id": trial_id,
        })

        workspace = self._workspace_manager.restore(repository, checkpoint.snapshot_path)
        state = None
        try:
            # The workspace manager copies the frozen tree into a new isolated
            # path; no in-place overwrite or Runtime-state reconstruction occurs.
            starting_fingerprint = checkpoint.workspace_fingerprint(workspace.path)
            if starting_fingerprint != payload["workspace"]["content_fingerprint"]:
                raise ValueError("restored workspace fingerprint differs from checkpoint")
            workspace_id = "workspace:" + hashlib.sha256(
                f"{trial_id}:{starting_fingerprint}".encode("utf-8")
            ).hexdigest()[:24]
            bind_identity = getattr(self._coding_agent, "bind_execution_identity", None)
            if callable(bind_identity):
                bind_identity(workspace_id=workspace_id, run_id=run_id, trial_id=trial_id)
            try:
                head = __import__("subprocess").run(
                    ["git", "-C", str(workspace.path), "rev-parse", "HEAD"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
            except (OSError, __import__("subprocess").CalledProcessError) as error:
                raise ValueError(f"restored checkpoint is not a usable Git workspace: {error}") from error
            if head != payload["fixture"]["baseline_revision"]:
                raise ValueError("restored workspace baseline revision differs from checkpoint")

            state = AgentState.from_dict(payload["agent_state"])
            # A new trial/run identity is bound to the same frozen continuation
            # state. Neither action count nor prior failure evidence is cleared.
            state.run_id = run_id
            state.workspace_reference = str(workspace.path)
            state.current_goal = "Repair validator failures"
            state.plan_reference = f"repair:{budgets['repairs_consumed'] + 1}"
            state.retry_count += 1
            # The checkpoint's terminal producer failure remains immutable in
            # its ValidationAttempt. It is not the active failure of this new
            # continuation run and must not mask a later repair decision error.
            state.failure = None
            state.termination_reason = ""
            state.validation_result = "NOT_RUN"
            state.final_outcome = ""
            for description in payload["validation_attempt"].get("repair_issue_descriptions", ()):
                state.issues.append(Issue.create("validation_failure", str(description)))
            state.transition("REPAIRING")
            self._observe_runtime_state(state)
            trace.emit("repair_started", {
                "checkpoint_id": checkpoint.checkpoint_id,
                "trial_id": trial_id,
                "failure_ids": [item.failure_id for item in failures],
                "repair_context_included": bool(include_repair_context),
                "steps_consumed": state.step_count,
                "steps_total": budgets["steps_total"],
                "repairs_consumed": budgets["repairs_consumed"],
                "repairs_total": budgets["repairs_total"],
            })

            repair_count = int(budgets["repairs_consumed"])
            validation_attempts = []
            executions = []
            last_validation = None
            previous_failure_ids = tuple(item.failure_id for item in failures)
            seen_action_ids = set(payload["validation_attempt"].get("action_ids", ()))
            decision_agent = _CheckpointRepairDecisionAgent(
                self._coding_agent, failures, self._observe_runtime_state,
            )
            while True:
                state.termination_reason = ""
                state = self._run_runtime_loop(
                    state, context, workspace.path, trace, coding_agent=decision_agent,
                )
                self._observe_runtime_state(state)
                repair_count += 1
                if not self._is_validation_eligible(state):
                    detail = state.errors[-1] if state.errors else "repair Runtime did not reach validation"
                    trace.emit("task_finished", {"status": "ERROR", "error": detail})
                    trace.write()
                    result = self._failure(
                        f"repair Runtime failed: {detail}",
                        contract=contract,
                        repo_context=context.repository_context,
                        plan=plan,
                        executions=tuple(executions),
                        repairs=repair_count,
                        runtime_failure=state.failure,
                        termination_reason=(
                            state.failure.category if state.failure else state.termination_reason
                        ),
                    )
                    break

                self._transition_runtime_state(state, "VERIFYING", trace)
                self._observe_runtime_state(state)
                last_validation = self._validate(contract, workspace.path)
                if isinstance(last_validation, str):
                    trace.emit("task_finished", {"status": "ERROR", "error": last_validation})
                    trace.write()
                    result = self._failure(
                        last_validation, contract=contract,
                        repo_context=context.repository_context, plan=plan,
                        executions=tuple(executions), repairs=repair_count,
                        validation_attempts=tuple(validation_attempts),
                    )
                    break

                attempt = self._validation_attempt(
                    last_validation,
                    contract,
                    run_id,
                    len(validation_attempts) + 1,
                    repair_failure_ids=previous_failure_ids,
                    action_ids=tuple(
                        action.id for action in state.recent_actions
                        if action.id not in seen_action_ids
                    ),
                )
                validation_attempts.append(attempt)
                self._observe_validation_attempt(attempt)
                seen_action_ids.update(attempt.action_ids)
                previous_failure_ids = tuple(item.failure_id for item in attempt.failure_contexts)
                trace.emit("validation_failed" if attempt.result.status == "FAIL" else "task_finished", {
                    "status": attempt.result.status,
                    "attempt_id": attempt.attempt_id,
                    "findings": [item.code for item in attempt.result.findings],
                    "failure_ids": [item.failure_id for item in attempt.failure_contexts],
                    "files": list(attempt.evidence.change_context.changed_files),
                })

                if attempt.result.status == "FAIL":
                    memory.record_failure(self._failure_record(run_id, task, contract, last_validation))
                if (
                    attempt.result.status != "FAIL"
                    or repair_count >= budgets["repairs_total"]
                    or any(item.execution_failed for item in attempt.evidence.verification)
                    or not attempt.evidence.workspace_is_consistent
                    or any(item.code in {"ASSERTION_UNSUPPORTED", "expected_change_evidence_missing"}
                           for item in attempt.result.findings)
                    or not self._runtime_repair
                ):
                    break

                context = self._context_builder.with_repair_context(
                    context,
                    attempt.failure_contexts,
                )
                if self._observer is not None:
                    self._observer.on_context(context)
                state.retry_count += 1
                state.plan_reference = f"repair:{repair_count + 1}"
                state.current_goal = "Repair validator failures"
                self._transition_runtime_state(state, "REPAIRING", trace)

            if last_validation is not None and not isinstance(last_validation, str):
                state.validation_result = last_validation.result.status
                state.final_outcome = {
                    "PASS": "SUCCEEDED", "WARNING": "COMPLETED_WITH_WARNINGS", "FAIL": "FAILED",
                }[last_validation.result.status]
                if last_validation.result.status == "PASS":
                    self._transition_runtime_state(state, "SUCCEEDED", trace)
                elif last_validation.result.status == "WARNING":
                    self._transition_runtime_state(state, "FINISHED", trace)
                else:
                    state.set_failure(RuntimeFailureRecord(
                        stage="VALIDATION", category="VALIDATOR_FAILED",
                        detail="validator rejected the resulting workspace",
                    ))
                    self._transition_runtime_state(state, "FAILED", trace)
                self._observe_runtime_state(state)
                patch_path = self._workspace_manager.save_patch(workspace)
                self._record(
                    memory, run_id, task, context.repository_context, contract, plan,
                    executions, last_validation, repair_count, patch_path, context, trace,
                )
                result = HarnessRunResult(
                    contract_path=None,
                    contract=contract,
                    repo_context=context.repository_context,
                    plan=plan,
                    report=last_validation.report,
                    executions=tuple(executions),
                    repairs=repair_count,
                    patch_path=patch_path,
                    validation_evidence=last_validation.evidence,
                    validator_result=last_validation.result,
                    failure=(None if last_validation.result.status == "PASS" else state.failure),
                    termination_reason=state.termination_reason,
                    validation_result=last_validation.result.status,
                    final_outcome=state.final_outcome,
                    validation_attempts=tuple(validation_attempts),
                )

            export_path = Path(final_workspace_path).resolve()
            if export_path.exists():
                raise ValueError(f"final workspace export already exists: {export_path}")
            export_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(workspace.path, export_path)
            final_fingerprint = checkpoint.workspace_fingerprint(export_path)
            return RepairStartResult(
                harness_result=result,
                runtime_state=AgentState.from_dict(state.to_dict()),
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_validation_attempt_id=payload["validation_attempt"]["attempt_id"],
                run_id=run_id,
                workspace_id=workspace_id,
                starting_workspace_fingerprint=starting_fingerprint,
                final_workspace_path=export_path,
                final_workspace_fingerprint=final_fingerprint,
            )
        finally:
            self._workspace_manager.cleanup(workspace)

    def _run_runtime_loop(self, state, context, workspace, trace, *, coding_agent=None):
        return AgentLoop(
            self._coding_agent if coding_agent is None else coding_agent,
            self._tool_registry,
            trace,
            max_steps=self._runtime_max_steps,
            history_manager=(
                self._history_manager_factory()
                if self._history_manager_factory is not None
                else HistoryManager()
            ),
            context_builder=self._context_builder,
            action_validator=(
                self._action_validator_factory()
                if self._action_validator_factory is not None
                else ActionValidator(self._execution_mode)
            ),
            ptc_runtime=(
                PTCCodeRuntime(tool_registry=self._tool_registry)
                if self._execution_mode == "PTC"
                else None
            ),
        ).run(state, context, workspace)

    @staticmethod
    def _is_validation_eligible(state: AgentState) -> bool:
        """Allow only the established finish path and stable NO_PROGRESS stops."""
        return state.status == "MODEL_FINISHED" or (
            state.status == "STOPPED" and state.termination_reason == "NO_PROGRESS"
        )

    @staticmethod
    def _repair_issue_description(finding, evidence: ValidationEvidence) -> str:
        description = f"{finding.code}: {finding.message}"
        if finding.code not in {"verification_failed", "verification_execution_failed"}:
            return description
        failures = []
        for verification in evidence.verification:
            if verification.passed:
                continue
            output = str(
                verification.execution_result.metadata.get("stdout_preview")
                or verification.execution_result.metadata.get("stderr_preview")
                or verification.execution_result.summary
                or ""
            ).strip()[:2000]
            failures.append(
                "command="
                + " ".join(verification.argv)
                + f"; exit={verification.actual_result.get('returncode')}; output={output}"
            )
        return description if not failures else f"{description}\n" + "\n".join(failures)

    def _transition_runtime_state(self, state, status, trace) -> None:
        state.transition(status)
        trace.emit(
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

    def _observe_runtime_state(self, state: AgentState) -> None:
        if self._observer is not None:
            self._observer.on_runtime_state(AgentState.from_dict(state.to_dict()))

    def _observe_validation_attempt(self, attempt: ValidationAttempt) -> None:
        callback = getattr(self._observer, "on_validation_attempt", None)
        if callable(callback):
            callback(attempt)

    def _write_contract(self, task_file: Path, contract: ChangeContract) -> Path:
        contract_path = Path(task_file).with_name("contract.yaml")
        if contract_path.exists():
            raise FileExistsError(
                f"refusing to overwrite existing Contract: {contract_path}"
            )
        contract_path.write_text(
            yaml.safe_dump(
                contract.model_dump(mode="json"),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return contract_path

    def _validate(self, contract, repo_path):
        try:
            before = collect_workspace_snapshot(repo_path)
            verification = execute_verification_claims(
                tuple(contract.verification.claims),
                self._verification_executor(),
                repo_path,
            )
            after = collect_workspace_snapshot(repo_path)
            evidence = ValidationEvidence.from_snapshots(before, after, verification)
        except GitContextError as error:
            return f"unable to collect Git context: {error}"
        except ValidationEvidenceError as error:
            return f"unable to construct validation evidence: {error}"
        assertion_results = tuple(
            AssertionEvaluatorRegistry().evaluate(assertion, evidence)
            for assertion in contract.verification.assertions
        )
        validation = self._validator.validate(
            contract,
            evidence,
            assertion_results=assertion_results,
        )
        return _WorkspaceValidation(
            evidence=evidence,
            result=validation,
            report=build_report(evidence, validation, contract=contract),
        )

    def _verification_executor(self) -> CanonicalActionExecutor:
        """Use the same governed command path for system verification actions."""
        return CanonicalActionExecutor(
            self._tool_registry,
            action_validator=(
                self._action_validator_factory()
                if self._action_validator_factory is not None
                else ActionValidator(self._execution_mode)
            ),
        )

    @staticmethod
    def _validation_attempt(validation_run, contract, run_id, number, *, repair_failure_ids=(), action_ids=()):
        attempt_id = f"{run_id}:validation:{number}"
        return ValidationAttempt(
            attempt_id=attempt_id, evidence=validation_run.evidence, result=validation_run.result,
            failure_contexts=build_repair_contexts(contract, validation_run.evidence, validation_run.result,
                                                 attempt_id=attempt_id),
            repair_failure_ids=repair_failure_ids, action_ids=action_ids,
        )

    def _record(
        self,
        memory,
        run_id,
        task,
        repo_context,
        contract,
        plan,
        executions,
        validation_run: _WorkspaceValidation,
        repairs,
        patch_path,
        context: StaticAgentContext,
        trace: TraceManager,
    ):
        trace.emit(
            "task_finished",
            {
                "status": validation_run.result.status,
                "repairs": repairs,
                "patch_path": str(patch_path),
            },
        )
        trace_path = trace.write()
        memory.record_decision(
            {
                "run_id": run_id,
                "task": task.strip(),
                "repair_attempts": repairs,
                "patch_path": str(patch_path),
                "trace_path": str(trace_path),
            }
        )
        if validation_run.result.status == "PASS":
            memory.confirm_success(
                run_id, validation_run.evidence.change_context.changed_files
            )

    def _agent_failure(
        self,
        execution,
        contract_path,
        contract,
        repo_context,
        plan,
        report,
        executions,
        repairs,
        memory,
        run_id,
        task,
        context,
        trace: TraceManager,
        validation_attempts: Tuple[ValidationAttempt, ...] = (),
        runtime_state: Optional[AgentState] = None,
    ) -> HarnessRunResult:
        memory.record_decision(
            {
                "task": "coding agent execution",
                "stderr": execution.stderr,
                "returncode": execution.returncode,
            }
        )
        trace.emit(
            "task_finished",
            {"status": "ERROR", "repairs": repairs, "error": _agent_error(execution)},
        )
        trace.write()
        return self._failure(
            _agent_error(execution),
            contract_path=contract_path,
            contract=contract,
            repo_context=repo_context,
            plan=plan,
            report=report,
            executions=tuple(executions),
            repairs=repairs,
            validation_attempts=validation_attempts,
            runtime_failure=runtime_state.failure if runtime_state is not None else None,
            termination_reason=(
                runtime_state.failure.category if runtime_state is not None and runtime_state.failure
                else runtime_state.termination_reason if runtime_state is not None else ""
            ),
            validation_result=validation_attempts[-1].result.status if validation_attempts else "NOT_RUN",
            validation_evidence=validation_attempts[-1].evidence if validation_attempts else None,
            validator_result=validation_attempts[-1].result if validation_attempts else None,
        )

    def _failure(
        self,
        error: str,
        contract_path: Optional[Path] = None,
        contract: Optional[ChangeContract] = None,
        repo_context: Optional[RepoContext] = None,
        plan: Optional[Plan] = None,
        report: Optional[ChangeReport] = None,
        executions: Tuple[CodingAgentResult, ...] = (),
        repairs: int = 0,
        runtime_failure: Optional[RuntimeFailureRecord] = None,
        termination_reason: str = "",
        validation_result: str = "NOT_RUN",
        validation_attempts: Tuple[ValidationAttempt, ...] = (),
        validation_evidence: Optional[ValidationEvidence] = None,
        validator_result: Optional[ValidationResult] = None,
    ) -> HarnessRunResult:
        return HarnessRunResult(
            contract_path=contract_path,
            contract=contract,
            repo_context=repo_context,
            plan=plan,
            report=report,
            executions=executions,
            repairs=repairs,
            patch_path=None,
            error=error,
            failure=runtime_failure or RuntimeFailureRecord(
                stage="INFRASTRUCTURE",
                category="HARNESS_ERROR",
                detail=error[:512],
            ),
            termination_reason=termination_reason,
            validation_result=validation_result,
            final_outcome=(
                "INFRASTRUCTURE_FAILED"
                if runtime_failure is None or runtime_failure.stage == "INFRASTRUCTURE"
                else "FAILED"
            ),
            validation_attempts=validation_attempts,
            validation_evidence=validation_evidence,
            validator_result=validator_result,
        )

    def _progress(self, callback, step, message):
        if callback is not None:
            callback(f"[{step}/8] {message}")

    def _failure_record(self, run_id, task, contract, validation_run: _WorkspaceValidation):
        return FailureRecord(
            run_id=run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            task=task.strip(),
            validator_findings=tuple(
                {
                    "rule": finding.rule,
                    "code": finding.code,
                    "message": finding.message,
                    "path": finding.path,
                    "severity": finding.severity,
                }
                for finding in validation_run.result.findings
            ),
            changed_files=validation_run.evidence.change_context.changed_files,
            contract_summary={
                "expected_scope_paths": list(contract.scope.paths),
                "forbidden_files": list(contract.constraints.forbidden),
                "change_level": contract.scope.change_level,
            },
        )

    def _default_tool_registry(
        self,
        approval_provider: Optional[ApprovalProvider],
        hard_guards: Optional[Tuple[HardGuard, ...]],
    ) -> ToolRegistry:
        registry = ToolRegistry(
            policy=ToolPolicy(),
            approval_provider=approval_provider,
            hard_guards=hard_guards,
        )
        registry.register(FilesystemTool())
        registry.register(GitTool())
        registry.register(ShellTool())
        return registry


def _agent_error(result: CodingAgentResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "no output"
    return f"coding agent failed (exit: {result.returncode}): {detail}"
