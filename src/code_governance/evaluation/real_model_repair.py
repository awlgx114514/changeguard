"""Real Codex A/B evaluation of the existing evidence-driven repair loop.

This is evaluation-only orchestration. Both conditions use the same Harness,
Runtime, tools, approval policy, model configuration, and step budgets; the
baseline ContextBuilder only removes the existing repair_context projection.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import perf_counter
import tempfile
from typing import Any
import uuid

from code_governance.agent.codex_agent import CodexAgent
from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.context.builder import ContextBuilder
from code_governance.contract.assertion import AssertionType
from code_governance.contract.schema import (
    ClaimCoverage,
    ExpectedChange,
    Scope,
    Verification,
    VerificationClaim,
)
from code_governance.evaluation.runner import BenchmarkRunner
from code_governance.evaluation.task import EvaluationTask
from code_governance.repo.analyzer import RepositoryAnalyzer
from code_governance.runtime.action import OperationKind
from code_governance.tools import ApprovalOutcome
from code_governance.workspace.manager import WorkspaceManager


FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "benchmarks" / "core_capability" / "real_model_repair"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[3] / "experiments" / "phase4d"
DECISION_TIMEOUT_SECONDS = 180
MAX_STEPS = 10
MAX_REPAIRS = 2


class _EmptyMemory:
    def active_rules(self):
        return ()

    def active_strategies(self):
        return ()


class _BaselineContextBuilder(ContextBuilder):
    """The only A/B difference: do not attach repair failure projections."""

    def with_repair_context(self, context, failures):
        return replace(context, repair_context=())


def _claim(claim_id: str, description: str, *argv: str) -> VerificationClaim:
    return VerificationClaim(
        id=claim_id,
        description=description,
        request={"argv": (sys.executable, "-B", *argv)},
        expected_result={"exit_code": 0},
    )


def real_model_repair_tasks(root: Path = FIXTURE_ROOT) -> tuple[EvaluationTask, ...]:
    """Construct explicit V3 contracts and argv for the three fixed scenarios."""
    pytest = ("-m", "pytest", "-q", "-p", "no:cacheprovider")

    behavior_ordinary = _claim(
        "password_regression", "Preserve valid and invalid password behavior.",
        *pytest, "tests/test_auth.py",
    )
    behavior_probe = _claim(
        "empty_password_probe", "Measure whether an empty password is accepted.",
        "tests/probes/empty_password_probe.py",
    )
    behavior = EvaluationTask(
        id="behavior_repair",
        description=(
            "Prevent empty passwords from being accepted while preserving the "
            "existing behavior for known and incorrect passwords."
        ),
        repository_snapshot="behavior",
        expected_files=("src/auth.py",),
        changes=(ExpectedChange(
            id="reject_empty_password",
            description="Empty passwords must be rejected.",
        ),),
        verification=Verification(
            claims=[behavior_ordinary, behavior_probe],
            coverage=[
                ClaimCoverage(claim_id=behavior_ordinary.id, change_id="reject_empty_password", coverage_type="direct"),
                ClaimCoverage(claim_id=behavior_probe.id, change_id="reject_empty_password", coverage_type="direct"),
            ],
            assertions=[
                {
                    "id": "password_regression_exit",
                    "change_id": "reject_empty_password",
                    "claim_id": behavior_ordinary.id,
                    "assertion_type": AssertionType.EXIT_CODE.value,
                    "config": {"expected_exit_code": 0},
                },
                {
                    "id": "empty_password_behavior",
                    "change_id": "reject_empty_password",
                    "claim_id": behavior_probe.id,
                    "assertion_type": AssertionType.BEHAVIOR_PROBE.value,
                    "config": {"probe_id": "empty_password_probe", "expected": {"accepted": False}},
                },
            ],
        ),
        difficulty="low",
    )

    calculator_claim = _claim(
        "calculator_regression", "Check the calculator total and empty-input control.",
        *pytest, "tests/test_calculator.py",
    )
    verification = EvaluationTask(
        id="verification_repair",
        description="Fix calculate_total so it returns the arithmetic sum for every input list.",
        repository_snapshot="verification",
        expected_files=("src/calculator.py",),
        changes=(ExpectedChange(
            id="calculate_total_sum",
            description="calculate_total returns the sum of all input items, including zero for an empty list.",
        ),),
        verification=Verification(
            claims=[calculator_claim],
            coverage=[ClaimCoverage(claim_id=calculator_claim.id, change_id="calculate_total_sum", coverage_type="direct")],
            assertions=[{
                "id": "calculator_exit",
                "change_id": "calculate_total_sum",
                "claim_id": calculator_claim.id,
                "assertion_type": AssertionType.EXIT_CODE.value,
                "config": {"expected_exit_code": 0},
            }],
        ),
        difficulty="low",
    )

    location_claim = _claim(
        "retry_delay_regression", "Check configured retry delay and zero-attempt control.",
        *pytest, "tests/test_service.py",
    )
    wrong_location = EvaluationTask(
        id="wrong_location_repair",
        description=(
            "Fix retry_delay so an attempt uses the configured two-second delay "
            "per retry. For attempt 3 the delay must be six seconds. The value "
            "is declared in src/config.py; inspect the configuration and service path."
        ),
        repository_snapshot="wrong_location",
        expected_files=("src/config.py", "src/service.py"),
        changes=(ExpectedChange(
            id="scale_retry_delay",
            description="Retry delay equals the configured seconds per attempt without changing the configuration.",
        ),),
        verification=Verification(
            claims=[location_claim],
            coverage=[ClaimCoverage(claim_id=location_claim.id, change_id="scale_retry_delay", coverage_type="direct")],
            assertions=[{
                "id": "retry_delay_exit",
                "change_id": "scale_retry_delay",
                "claim_id": location_claim.id,
                "assertion_type": AssertionType.EXIT_CODE.value,
                "config": {"expected_exit_code": 0},
            }],
        ),
        difficulty="low",
    )
    return behavior, verification, wrong_location


class _ScopedFixtureApproval:
    """Resolve only exact local fixture writes and declared system claims."""

    def __init__(self, task: EvaluationTask):
        self.workspace: Path | None = None
        self.allowed_paths = {
            "behavior_repair": {"src/auth.py"},
            "verification_repair": {"src/calculator.py"},
            "wrong_location_repair": {"src/config.py", "src/service.py"},
        }[task.id]
        self.claim_ids = {claim.to_request().id for claim in task.verification.claims}
        self.decisions: list[dict[str, Any]] = []

    def bind_workspace(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()

    def request(self, request):
        allowed = False
        if request.effect in {"file.replace", "file.write"}:
            try:
                if self.workspace is not None:
                    relative = Path(request.canonical_target).resolve().relative_to(self.workspace).as_posix()
                    allowed = relative in self.allowed_paths
            except (OSError, ValueError):
                allowed = False
        elif request.effect == "command.run":
            allowed = request.action_id in self.claim_ids
        outcome = ApprovalOutcome.APPROVED if allowed else ApprovalOutcome.REJECTED
        self.decisions.append({**asdict(request), "outcome": outcome.value})
        return outcome


class _ApprovalBoundWorkspaceManager(WorkspaceManager):
    """Keep the Harness workspace copy and bind approvals to that exact copy."""

    def __init__(self, approval: _ScopedFixtureApproval):
        self._approval = approval

    def create(self, repo_path: Path):
        workspace = super().create(repo_path)
        self._approval.bind_workspace(workspace.path)
        return workspace

    def restore(self, repo_path: Path, snapshot_path: Path):
        workspace = super().restore(repo_path, snapshot_path)
        self._approval.bind_workspace(workspace.path)
        return workspace


class _RecordingCodexAgent(CodexAgent):
    def __init__(self, timeout: int):
        super().__init__(timeout=timeout)
        self.decisions: list[dict[str, Any]] = []
        self._pending: dict[str, Any] | None = None

    def decide(self, state, context):
        repair_ids = [item.failure_id for item in context.repair_context]
        record = {
            "index": len(self.decisions) + 1,
            "repair_context_ids": repair_ids,
            "repair_context_visible": None,
            "response_received": False,
            "action": None,
            "error_class": None,
            "error": None,
        }
        self._pending = record
        started = perf_counter()
        try:
            action = super().decide(state, context)
            record["response_received"] = True
            record["action"] = action.to_dict()
            record["action_kind"] = action.operation.kind.value
            return action
        except Exception as error:
            record["error_class"] = type(error).__name__
            record["error"] = str(error)[:512]
            category = getattr(error, "category", "")
            record["error_category"] = str(category)[:128]
            raise
        finally:
            record["duration_seconds"] = round(perf_counter() - started, 3)
            self.decisions.append(record)
            self._pending = None

    def _run_cli(self, command, *, prompt, workspace):
        record = self._pending
        ids = () if record is None else record["repair_context_ids"]
        if record is not None:
            record["repair_context_visible"] = bool(ids) and all(item in prompt for item in ids)
            record["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        completed = super()._run_cli(command, prompt=prompt, workspace=workspace)
        if record is not None:
            event_types, item_types, native_items, models = _summarize_provider_events(completed.stdout)
            record["provider_event_types"] = event_types
            record["provider_item_types"] = item_types
            record["native_tool_call_count"] = native_items
            record["model_reported_by_provider"] = models[0] if models else None
        return completed


def _summarize_provider_events(stdout: str) -> tuple[list[str], list[str], int, list[str]]:
    events: set[str] = set()
    items: set[str] = set()
    native_ids: set[str] = set()
    models: set[str] = set()
    native_types = {
        "command_execution", "file_change", "mcp_tool_call", "web_search_call",
        "local_shell_call", "function_call", "computer_call",
    }
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if isinstance(kind, str):
            events.add(kind)
        item = event.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if isinstance(item_type, str):
                items.add(item_type)
                if item_type in native_types:
                    native_ids.add(str(item.get("id") or (kind, item_type)))
        for key in ("model", "model_name", "model_slug"):
            value = event.get(key)
            if isinstance(value, str) and value:
                models.add(value)
    return sorted(events), sorted(items), len(native_ids), sorted(models)


class _ContextCapture:
    def __init__(self, delegate):
        self.delegate = delegate
        self.contexts: list[dict[str, Any]] = []
        self.states: list[dict[str, Any]] = []

    def on_context(self, context):
        self.contexts.append({
            "repair_failure_ids": [item.failure_id for item in context.repair_context],
            "repair_context_count": len(context.repair_context),
        })
        self.delegate.on_context(context)

    def on_runtime_state(self, state):
        self.states.append({
            "status": state.status,
            "action_ids": [item.id for item in state.recent_actions],
            "observation_summaries": [
                {"action_id": item.action_id, "type": item.type, "success": item.success}
                for item in state.recent_observations
            ],
            "failure": None if state.failure is None else state.failure.to_dict(),
        })
        self.delegate.on_runtime_state(state)


def _oracle(task_id: str, root: Path) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    sys.path.insert(0, str(root))
    try:
        if task_id == "behavior_repair":
            namespace.update(__import__("runpy").run_path(str(root / "src" / "auth.py")))
            observed = {"empty_password_accepted": bool(namespace["check_password"](""))}
            expected = {"empty_password_accepted": False}
        elif task_id == "verification_repair":
            namespace.update(__import__("runpy").run_path(str(root / "src" / "calculator.py")))
            observed = {"total": namespace["calculate_total"]([1, 2, 3]), "empty_total": namespace["calculate_total"]([])}
            expected = {"total": 6, "empty_total": 0}
        else:
            namespace.update(__import__("runpy").run_path(str(root / "src" / "service.py")))
            config = __import__("runpy").run_path(str(root / "src" / "config.py"))
            observed = {"retry_delay_attempt_3": namespace["retry_delay"](3),
                        "configured_seconds": config["configured_retry_delay"]()}
            expected = {"retry_delay_attempt_3": 6, "configured_seconds": 2}
        return {"observed": observed, "expected": expected, "goal_satisfied": observed == expected}
    finally:
        if sys.path and sys.path[0] == str(root):
            sys.path.pop(0)


def _attempt_record(attempt) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "repair_failure_ids": list(attempt.repair_failure_ids),
        "action_ids": list(attempt.action_ids),
        "failure_contexts": [item.to_dict() for item in attempt.failure_contexts],
        "validation_status": attempt.result.status,
        "findings": [{"code": item.code, "severity": item.severity, "path": item.path}
                     for item in attempt.result.findings],
        "verification": [{
            "claim_id": item.claim_id,
            "request_id": item.request_id,
            "action_id": item.action_id,
            "execution_result_id": item.execution_result_id,
            "passed": item.passed,
            "execution_status": item.execution_result.status,
            "returncode": item.actual_result.get("returncode"),
        } for item in attempt.evidence.verification],
        "workspace_identity": asdict(attempt.evidence.workspace),
    }


def _failure_classification(result, decisions) -> dict[str, str] | None:
    failed_decision = next((item for item in decisions if not item["response_received"]), None)
    if failed_decision is not None:
        category = failed_decision.get("error_category") or ""
        provider_categories = {"codex_cli_launch_failed", "codex_cli_nonzero_exit", "codex_cli_timeout"}
        if category in provider_categories:
            return {"category": "ENVIRONMENT", "detail": category}
        return {"category": "ADAPTER", "detail": category or failed_decision.get("error_class", "decision_failed")}
    failure = result.failure
    if failure is not None:
        stage = str(failure.stage).upper()
        if stage == "TOOL":
            label = "TOOL"
        elif stage == "ENVIRONMENT":
            label = "ENVIRONMENT"
        elif stage == "ADAPTER":
            label = "ADAPTER"
        elif stage == "RUNTIME":
            label = "RUNTIME"
        else:
            label = "RUNTIME"
        return {"category": label, "detail": str(failure.category)}
    validation = result.validator_result
    if validation is not None and validation.status != "PASS":
        codes = {item.code for item in validation.findings}
        category = "VERIFICATION" if codes.intersection({"verification_failed", "verification_execution_failed"}) else "VALIDATION"
        return {"category": category, "detail": ",".join(sorted(codes))[:256]}
    if not result.success:
        return {"category": "MODEL", "detail": "goal_not_validated"}
    return None


def _action_relevance(task_id: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    relevant = {
        "behavior_repair": {"src/auth.py"},
        "verification_repair": {"src/calculator.py"},
        "wrong_location_repair": {"src/service.py"},
    }[task_id]
    seen: set[tuple[str, str]] = set()
    irrelevant = 0
    context_used = False
    action_count = 0
    for decision in decisions:
        action = decision.get("action")
        if not isinstance(action, dict):
            continue
        action_count += 1
        kind = action.get("operation", decision.get("action_kind", ""))
        target = str(action.get("path", "")).replace("\\", "/")
        if kind in {OperationKind.FILE_REPLACE.value, OperationKind.FILE_WRITE.value, OperationKind.FILE_CREATE.value}:
            if target not in relevant:
                irrelevant += 1
        signature = (str(kind), target)
        if signature in seen and kind != OperationKind.WORKFLOW_FINISH.value:
            irrelevant += 1
        seen.add(signature)
        refs = decision.get("repair_context_changed_files", ())
        if target and target in refs and kind in {
            OperationKind.FILE_READ.value, OperationKind.FILE_REPLACE.value,
            OperationKind.FILE_WRITE.value, OperationKind.FILE_SEARCH.value,
        }:
            context_used = True
    return {
        "action_count": action_count,
        "irrelevant_action_count": irrelevant,
        "irrelevant_action_rate": (irrelevant / action_count if action_count else 0.0),
        "repair_context_used": context_used,
    }


def _run_trial(task: EvaluationTask, condition: str, repeat: int, timeout: int) -> dict[str, Any]:
    evaluation_root = DEFAULT_OUTPUT
    evaluation_root.mkdir(parents=True, exist_ok=True)
    workspace_parent = evaluation_root / "workspaces"
    workspace_parent.mkdir(parents=True, exist_ok=True)
    run_id = f"{task.id}-{condition}-{repeat}-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(prefix=f"{run_id}-", dir=workspace_parent) as temporary:
        parent = Path(temporary)
        workspace = parent / "repository"
        shutil.copytree(FIXTURE_ROOT / task.repository_snapshot, workspace)
        baseline = BenchmarkRunner._initialize_repository(workspace)
        task_path = parent / "task.md"
        task_path.write_text(task.description, encoding="utf-8")
        approval = _ScopedFixtureApproval(task)
        agent = _RecordingCodexAgent(timeout=timeout)
        capture = _ContextCapture(_NullObserver())
        context_builder = _BaselineContextBuilder() if condition == "baseline" else ContextBuilder()
        result = HarnessOrchestrator(
            contract_agent=ContractAgent(generator=lambda _task, _repo: task.to_contract()),
            coding_agent=agent,
            context_builder=context_builder,
            workspace_manager=_ApprovalBoundWorkspaceManager(approval),
            approval_provider=approval,
            max_repairs=MAX_REPAIRS,
            runtime_max_steps=MAX_STEPS,
            observer=capture,
        ).run(task_path, workspace)
        oracle = _oracle(task.id, workspace)
        attempts = [_attempt_record(item) for item in result.validation_attempts]
        trajectory = [item for item in agent.decisions]
        changed_by_failure = {
            failure["failure_id"]: failure["changed_files"]
            for attempt in attempts for failure in attempt["failure_contexts"]
        }
        for decision in trajectory:
            decision["repair_context_changed_files"] = [
                path for failure_id in decision["repair_context_ids"]
                for path in changed_by_failure.get(failure_id, ())
            ] if decision["repair_context_ids"] else []
        relevance = _action_relevance(task.id, trajectory)
        all_native_calls = sum(item.get("native_tool_call_count", 0) for item in trajectory)
        trace_paths = sorted((workspace / ".agent" / "runs").glob("*/trace.json"))
        trace = None
        if len(trace_paths) == 1:
            try:
                trace = json.loads(trace_paths[0].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                trace = None
        contexts = capture.contexts
        first_context_ids = [failure.failure_id for failure in result.validation_attempts[0].failure_contexts] if result.validation_attempts else []
        repair_decisions = [item for item in trajectory if item["repair_context_ids"]]
        repair_context_visible = bool(repair_decisions) and any(item["repair_context_visible"] for item in repair_decisions)
        changed_files = sorted({path for attempt in result.validation_attempts for path in attempt.evidence.change_context.changed_files})
        record = {
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "fixture": task.id,
            "condition": condition,
            "repeat": repeat,
            "model": next((item["model_reported_by_provider"] for item in trajectory if item.get("model_reported_by_provider")),
                          _configured_model()),
            "codex_cli_version": None,
            "model_configuration": "current Codex CLI profile; no per-run model or temperature override",
            "max_steps": MAX_STEPS,
            "max_repairs": MAX_REPAIRS,
            "decision_timeout_seconds": timeout,
            "provider_decisions": trajectory,
            "repair_context_received": bool(first_context_ids),
            "repair_context_visible": repair_context_visible,
            "repair_context_used": relevance["repair_context_used"] if repair_context_visible else False,
            "validation_attempts": attempts,
            "actions": [item.get("action") for item in trajectory if item.get("action")],
            "action_count": relevance["action_count"],
            "irrelevant_action_count": relevance["irrelevant_action_count"],
            "irrelevant_action_rate": relevance["irrelevant_action_rate"],
            "changed_files": changed_files,
            "oracle_after": oracle,
            "harness_result": {
                "success": result.success,
                "final_outcome": result.final_outcome,
                "termination_reason": result.termination_reason,
                "validation_result": result.validation_result,
                "failure": None if result.failure is None else result.failure.to_dict(),
                "error": None if result.error is None else result.error[:512],
            },
            "failure_classification": _failure_classification(result, trajectory),
            "native_tool_call_count": all_native_calls,
            "canonical_path_preserved": all_native_calls == 0,
            "context_observations": contexts,
            "runtime_observation_summaries": capture.states,
            "trace": trace,
            "direct_success": bool(
                result.success and oracle["goal_satisfied"] and len(result.validation_attempts) == 1
            ),
            "baseline_commit": baseline,
        }
        return record


class _NullObserver:
    def on_context(self, context):
        pass

    def on_runtime_state(self, state):
        pass


def _preflight() -> dict[str, Any]:
    try:
        version = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=15, check=False)
        auth = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"codex_cli_available": False, "authenticated": False, "error": type(error).__name__}
    return {
        "codex_cli_available": version.returncode == 0,
        "codex_cli_version": version.stdout.strip()[:80] if version.returncode == 0 else None,
        "authenticated": auth.returncode == 0,
        "model": _configured_model(),
    }


def _configured_model() -> str:
    """Read only the selected model name, never other user Codex settings."""
    path = Path.home() / ".codex" / "config.toml"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return "Codex CLI default (no model field found)"
    match = re.search(r"(?m)^\s*model\s*=\s*['\"]([^'\"]+)['\"]\s*$", content)
    return match.group(1) if match else "Codex CLI default (no model field found)"


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in records:
        groups.setdefault((item["fixture"], item["condition"]), []).append(item)
    result = {}
    for key, group in groups.items():
        eligible = [item for item in group if item["repair_context_received"]]
        result[f"{key[0]}:{key[1]}"] = {
            "success_rate": sum(bool(item["harness_result"]["success"] and item["oracle_after"]["goal_satisfied"]) for item in group) / len(group),
            "false_success_rate": sum(bool(item["harness_result"]["success"] and not item["oracle_after"]["goal_satisfied"]) for item in group) / len(group),
            "average_model_decisions": sum(len(item["provider_decisions"]) for item in group) / len(group),
            "average_action_count": sum(item["action_count"] for item in group) / len(group),
            "irrelevant_action_rate": sum(item["irrelevant_action_count"] for item in group) / max(1, sum(item["action_count"] for item in group)),
            "repair_context_usage_rate": (
                sum(bool(item["repair_context_used"]) for item in eligible) / len(eligible) if eligible else None
            ),
            "repair_context_eligible_runs": len(eligible),
        }
    return result


def run_real_model_repair_evaluation(
    *, repetitions: int = 3, timeout_seconds: int = DECISION_TIMEOUT_SECONDS,
    preflight_only: bool = False, output_dir: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    if repetitions < 1 or timeout_seconds < 1:
        raise ValueError("repetitions and timeout_seconds must be positive")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "evidence.json"
    preflight = _preflight()
    report: dict[str, Any] = {
        "phase": "PHASE_4D_DESIGN=REAL_MODEL_EVIDENCE_DRIVEN_REPAIR_EVALUATION",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider_preflight": preflight,
        "fixtures": [task.id for task in real_model_repair_tasks()],
        "repetitions_per_condition": repetitions,
        "max_steps": MAX_STEPS,
        "max_repairs": MAX_REPAIRS,
        "decision_timeout_seconds": timeout_seconds,
        "trials": [],
        "stopped_reason": None,
    }
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not preflight["codex_cli_available"] or not preflight["authenticated"]:
        report["stopped_reason"] = "PROVIDER_PREFLIGHT_FAILED"
        result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    tasks = real_model_repair_tasks()
    # The first treatment trial is also the live provider/context preflight.
    schedule = [(tasks[0], "treatment", 1)]
    if not preflight_only:
        for task in tasks:
            for repeat in range(1, repetitions + 1):
                for condition in ("baseline", "treatment"):
                    if task.id == tasks[0].id and condition == "treatment" and repeat == 1:
                        continue
                    schedule.append((task, condition, repeat))

    for task, condition, repeat in schedule:
        try:
            record = _run_trial(task, condition, repeat, timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as error:
            record = {
                "fixture": task.id, "condition": condition, "repeat": repeat,
                "harness_result": {"success": False, "error": f"{type(error).__name__}: {str(error)[:256]}"},
                "failure_classification": {"category": "ENVIRONMENT", "detail": type(error).__name__},
                "provider_decisions": [], "native_tool_call_count": 0,
                "canonical_path_preserved": True,
            }
        report["trials"].append(record)
        report["metrics"] = _summary(report["trials"])
        has_provider_failure = any(
            item.get("error_category") in {"codex_cli_launch_failed", "codex_cli_nonzero_exit", "codex_cli_timeout"}
            for item in record.get("provider_decisions", ())
        )
        has_native_bypass = not record.get("canonical_path_preserved", True)
        if has_provider_failure:
            report["stopped_reason"] = "PROVIDER_REQUEST_FAILED"
        elif has_native_bypass:
            report["stopped_reason"] = "PROVIDER_NATIVE_TOOL_CALL_OBSERVED"
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if report["stopped_reason"]:
            break
        if preflight_only:
            break
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=DECISION_TIMEOUT_SECONDS)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = run_real_model_repair_evaluation(
        repetitions=args.repetitions, timeout_seconds=args.timeout_seconds,
        preflight_only=args.preflight_only, output_dir=args.output_dir,
    )
    trials = report["trials"]
    print(json.dumps({
        "provider_preflight": report["provider_preflight"],
        "trials_completed": len(trials),
        "first_trial_direct_success": trials[0].get("direct_success") if trials else None,
        "repair_context_visible": any(
            decision.get("repair_context_visible") for trial in trials
            for decision in trial.get("provider_decisions", ())
        ),
        "stopped_reason": report["stopped_reason"],
        "evidence_file": str(args.output_dir / "evidence.json"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
