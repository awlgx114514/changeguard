"""Offline correctness scenarios, executed by the existing BenchmarkRunner.

Detailed records are opt-in fixture diagnostics, not additions to the generic
privacy-safe benchmark report. Independent oracles never enter Runtime evidence.
"""

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter

from code_governance.agent.harness import HarnessOrchestrator
from code_governance.contract.schema import ExpectedChange, Verification, VerificationClaim
from code_governance.runtime.action import Action, OperationKind
from code_governance.tools import ApprovalOutcome, ToolPolicy, ToolRegistry

from .dataset import EvaluationDataset
from .runner import BenchmarkRunner, _validation_failure_category
from .task import EvaluationTask


FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "benchmarks" / "core_capability"


def _claim(claim_id, description, *arguments):
    return VerificationClaim(
        id=claim_id, description=description,
        request={"argv": (sys.executable, "-B", *arguments)},
        expected_result={"exit_code": 0},
    )


def core_capability_dataset(root=FIXTURE_ROOT):
    """Build explicit V3 contracts; argv is never parsed from command text."""
    pytest_args = ("-m", "pytest", "-q", "-p", "no:cacheprovider")
    definitions = (
        ("true_success", "calculator", "fix_add_behavior",
         "Fix calculator.add so that it returns the correct sum and the regression test passes.",
         "calculator.add returns the mathematical sum", "calculator_regression"),
        ("false_evidence", "auth", "reject_empty_password",
         "Prevent empty passwords from being accepted.",
         "Empty passwords are rejected", "ordinary_auth_test"),
        ("false_finish", "feature", "implement_feature",
         'Implement feature() so that it returns "enabled".',
         'feature() returns "enabled"', "feature_test"),
    )
    tasks = []
    for task_id, module, change_id, goal, description, claim_id in definitions:
        claims = [_claim(claim_id, f"Run {module} regression tests", *pytest_args,
                         f"tests/test_{module}.py")]
        coverage = [{"claim_id": claim_id, "change_id": change_id,
                     "coverage_type": "indirect" if task_id == "false_evidence" else "direct"}]
        assertion = {"id": f"{change_id}_assertion", "change_id": change_id,
                     "claim_id": claim_id, "assertion_type": "exit_code", "config": {"expected_exit_code": 0}}
        if task_id == "false_evidence":
            claims.append(_claim("empty_password_probe", "Observe empty password acceptance",
                                 "tests/probes/check_empty_password.py"))
            coverage.append({"claim_id": "empty_password_probe", "change_id": change_id,
                             "coverage_type": "direct"})
            assertion.update(claim_id="empty_password_probe", assertion_type="behavior_probe",
                             config={"probe_id": "empty_password_probe", "expected": {"accepted": False}})
        tasks.append(EvaluationTask(
            id=task_id, description=goal, repository_snapshot=f"fixture_{task_id}",
            expected_files=(f"src/{module}.py",),
            changes=(ExpectedChange(id=change_id, description=description),), difficulty="low",
            verification=Verification(claims=claims, coverage=coverage, assertions=[assertion]),
        ))
    return EvaluationDataset(id="harness-core-capability-v1", tasks=tuple(tasks), dataset_root=root)


class _ScenarioAgent:
    """Deterministic decisions; only the real AgentLoop executes these Actions."""

    def __init__(self, task):
        self.task = task
        self.step = 0

    def decide(self, state, context):
        self.step += 1
        if self.task.id == "true_success":
            if self.step == 1:
                return Action.create_operation(OperationKind.FILE_READ, path="src/calculator.py",
                                               reason="Inspect the implementation before editing")
            if self.step == 2:
                observed = next(item for item in reversed(state.recent_observations)
                                if item.success and item.metadata.get("file_hash"))
                return replace(Action.create_operation(
                    OperationKind.FILE_REPLACE, path="src/calculator.py",
                    expected_text="return a - b", replacement="return a + b",
                    observed_file_hash=observed.metadata["file_hash"],
                    reason="Correct subtraction to addition using observed code"), id="calculator_replace")
            if self.step == 3:
                return replace(Action.create_operation(
                    OperationKind.COMMAND_RUN, argv=self.task.verification.claims[0].request.argv,
                    reason="Verify the local correction"), id="calculator_verify")
        return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="Request final validation")


class _FixtureApproval:
    """Explicitly approve only known scenario Actions, never default auto-approve."""

    def __init__(self, task):
        self.command_ids = {claim.to_request().id for claim in task.verification.claims}
        if task.id == "true_success":
            self.command_ids.add("calculator_verify")
        self.task = task
        self.records = []

    def request(self, request):
        allowed = (
            request.effect == "command.run" and request.action_id in self.command_ids
        ) or (
            self.task.id == "true_success" and request.effect == "file.replace"
            and request.action_id == "calculator_replace"
            and Path(request.canonical_target).as_posix().endswith("/src/calculator.py")
        )
        outcome = ApprovalOutcome.APPROVED if allowed else ApprovalOutcome.REJECTED
        self.records.append({**asdict(request), "outcome": outcome.value})
        return outcome


def _external_process(argv, repository):
    started = perf_counter()
    result = subprocess.run(argv, cwd=repository, capture_output=True, text=True,
                            timeout=20, check=False)
    return {"argv": list(argv), "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
            "duration_seconds": perf_counter() - started}


def _oracle(task_id, repository):
    # Independent direct API calls, not the Harness tests or behavior-probe parser.
    expressions = {
        "true_success": ("calculator", "{'sums': [m['add'](2, 3), m['add'](-2, 2), m['add'](-3, -2)]}",
                         "observed == {'sums': [5, 0, -5]}"),
        "false_evidence": ("auth", "{'accepted': bool(m['check_password'](''))}",
                           "observed == {'accepted': False}"),
        "false_finish": ("feature", "{'value': m['feature']()}",
                         "observed == {'value': 'enabled'}"),
    }
    module, expression, expected = expressions[task_id]
    code = (f"import json, runpy; m=runpy.run_path('src/{module}.py'); "
            f"observed={expression}; print(json.dumps({{'observed':observed, 'goal_satisfied':{expected}}}))")
    result = _external_process((sys.executable, "-B", "-c", code), repository)
    if result["returncode"] != 0:
        raise RuntimeError(f"Independent oracle failed to execute: {result['stderr']}")
    return {**result, **json.loads(result["stdout"])}


class _CaptureObserver:
    def __init__(self, delegate, task, capture):
        self.delegate = delegate
        self.task = task
        self.capture = capture
        self.statuses = []
        self.trajectory = []
        self.observations = []
        self.oracle_after = None

    def on_context(self, context):
        self.delegate.on_context(context)

    def on_runtime_state(self, state):
        self.delegate.on_runtime_state(state)
        self.statuses.append(state.status)
        self.trajectory = [action.to_dict() for action in state.recent_actions]
        self.observations = [item.to_dict() for item in state.recent_observations]
        if state.status in {"SUCCEEDED", "FAILED", "FINISHED", "STOPPED"}:
            # Observe the terminal workspace before Harness performs its normal cleanup.
            self.capture.boundary = "FIXTURE_ORACLE_AFTER"
            self.oracle_after = _oracle(self.task.id, Path(state.workspace_reference))
            self.capture.boundary = "HARNESS"


class _CaptureHarnessFactory:
    def __init__(self, task, *, max_repairs=0, approval_provider=None, context_builder=None):
        self.task = task
        self.max_repairs = max_repairs
        self.approval_provider = approval_provider
        self.context_builder = context_builder
        self.record = None
        self.error = None
        self.boundary = "FIXTURE_ORACLE_BEFORE"

    def __call__(self, **kwargs):
        owner = self

        class CaptureHarness:
            def run(self, task_file, repository):
                try:
                    return self._run(task_file, repository)
                except Exception as error:
                    # Preserve the diagnostic before BenchmarkRunner's existing
                    # exception projection; do not invent a Harness failure.
                    owner.error = {"task_id": owner.task.id, "boundary": owner.boundary,
                                   "error_type": type(error).__name__, "detail": str(error)}
                    raise

            def _run(self, task_file, repository):
                before = _oracle(owner.task.id, repository)
                owner.boundary = "FIXTURE_BASELINE_TEST"
                baseline_test = _external_process(owner.task.verification.claims[0].request.argv, repository)
                expected_exit = 0 if owner.task.id == "false_evidence" else 1
                self_valid = not before["goal_satisfied"] and baseline_test["returncode"] == expected_exit
                if not self_valid:
                    raise ValueError(f"Fixture self-validation failed: {owner.task.id}")
                observer = _CaptureObserver(kwargs["observer"], owner.task, owner)
                approval = owner.approval_provider or _FixtureApproval(owner.task)
                registry = ToolRegistry(policy=ToolPolicy(), approval_provider=approval)
                supplied_registry = kwargs["tool_registry"]
                for name in supplied_registry.list_tools():
                    registry.register(supplied_registry.get(name))
                owner.boundary = "HARNESS"
                if owner.context_builder is not None:
                    kwargs["context_builder"] = owner.context_builder
                result = HarnessOrchestrator(**{
                    **kwargs, "observer": observer, "tool_registry": registry, "max_repairs": owner.max_repairs,
                }).run(task_file, repository)
                owner.boundary = "EVALUATION_RESULT_CAPTURE"
                contract = result.contract.model_dump(mode="json")
                evidence = result.validation_evidence
                validation = result.validator_result
                owner.record = {
                    "task_id": owner.task.id, "fixture_type": owner.task.id,
                    "contract_version": contract["version"], "expected_changes": contract["changes"],
                    "claim_coverage": contract["verification"]["coverage"],
                    "verification_claims": contract["verification"]["claims"],
                    "assertions": contract["verification"]["assertions"],
                    "oracle_before": before, "oracle_after": observer.oracle_after,
                    "self_validation": {"passed": self_valid, "baseline_test": baseline_test},
                    "trajectory": observer.trajectory, "observations": observer.observations,
                    "runtime_statuses": observer.statuses, "approvals": approval.records,
                    "repairs": result.repairs,
                    "validation_attempts": [
                        {"attempt_id": attempt.attempt_id,
                         "workspace_identity": asdict(attempt.evidence.workspace),
                         "failure_contexts": [failure.to_dict() for failure in attempt.failure_contexts],
                         "repair_failure_ids": list(attempt.repair_failure_ids), "action_ids": list(attempt.action_ids),
                         "verification_evidence": [{**asdict(item), "execution_result": item.execution_result.to_dict()}
                                                   for item in attempt.evidence.verification],
                         "failure_boundary": (_validation_failure_category(attempt.result)
                                              if attempt.result.status == "FAIL" else None),
                         "validation_result": {"status": attempt.result.status,
                                               "findings": [asdict(item) for item in attempt.result.findings]},
                         "assertion_results": [asdict(item) for item in attempt.result.assertion_results]}
                        for attempt in result.validation_attempts
                    ],
                    "workspace_identity": asdict(evidence.workspace) if evidence else None,
                    "changed_files": list(evidence.change_context.changed_files) if evidence else [],
                    "verification_evidence": [
                        {**asdict(item), "execution_result": item.execution_result.to_dict()}
                        for item in evidence.verification
                    ] if evidence else [],
                    "assertion_results": [asdict(item) for item in validation.assertion_results] if validation else [],
                    "validation_result": {"status": validation.status,
                                          "findings": [asdict(item) for item in validation.findings]} if validation else None,
                    "harness_result": {"success": result.success, "error": result.error,
                                       "termination_reason": result.termination_reason,
                                       "final_outcome": result.final_outcome,
                                       "failure": result.failure.to_dict() if result.failure else None},
                }
                return result

        return CaptureHarness()


def _verified_changes(record):
    """Evaluation projection only: declared direct coverage and assertion facts."""
    passed_claims = {v["claim_id"] for v in record["verification_evidence"] if v["passed"]}
    return {
        change["id"]: (
            record["validation_result"] is not None
            and record["validation_result"]["status"] == "PASS"
            and any(c["change_id"] == change["id"] and c["coverage_type"] == "direct"
                    and c["claim_id"] in passed_claims for c in record["claim_coverage"])
            and all(a["status"] == "passed" for a in record["assertion_results"]
                    if a["change_id"] == change["id"])
        ) for change in record["expected_changes"] if change["requirement"] == "required"
    }


def _scenario_passed(record):
    """Require the intended observed boundary, not merely a negative outcome."""
    validation = record["validation_result"]
    if validation is None or record["oracle_after"] is None:
        return False
    evidence = record["verification_evidence"]
    assertions = record["assertion_results"]
    success = record["harness_result"]["success"]
    goal = record["oracle_after"]["goal_satisfied"]
    codes = {finding["code"] for finding in validation["findings"]}
    if record["task_id"] == "true_success":
        return (success and goal and validation["status"] == "PASS"
                and bool(evidence) and all(item["passed"] for item in evidence)
                and bool(assertions) and all(item["status"] == "passed" for item in assertions)
                and all(record["required_changes_verified"].values()))
    if record["task_id"] == "false_evidence":
        by_claim = {item["claim_id"]: item for item in evidence}
        ordinary = by_claim.get("ordinary_auth_test")
        probe = by_claim.get("empty_password_probe")
        return (not success and not goal and ordinary is not None and ordinary["passed"]
                and probe is not None and probe["execution_result"]["status"] == "SUCCESS"
                and probe["actual_result"].get("returncode") == 0 and probe["passed"]
                and any(item["assertion_type"] == "behavior_probe" and item["status"] == "failed"
                        for item in assertions)
                and "ASSERTION_FAILED" in codes and record["first_failing_boundary"] == "ASSERTION")
    return (not success and not goal and "MODEL_FINISHED" in record["runtime_statuses"]
            and "VERIFYING" in record["runtime_statuses"] and bool(evidence)
            and evidence[0]["execution_result"]["status"] == "SUCCESS"
            and not evidence[0]["passed"] and evidence[0]["actual_result"].get("returncode") != 0
            and "verification_failed" in codes and record["first_failing_boundary"] == "VERIFICATION")


def run_core_capability_evaluation(root=FIXTURE_ROOT):
    dataset = core_capability_dataset(root)
    records = []
    gap = None
    unresolved = []
    for task in dataset.tasks:
        capture = _CaptureHarnessFactory(task)
        trial = BenchmarkRunner(dataset, coding_agent_factory=lambda: _ScenarioAgent(task),
                                harness_factory=capture).run_task(task, "hardened")
        if capture.record is None:
            unresolved.append(capture.error or {"task_id": task.id,
                              "boundary": "EVALUATION_RESULT_CAPTURE", "detail": trial.failure_category})
            break
        record = capture.record
        record["benchmark_trial"] = trial.to_dict()
        record["first_failing_boundary"] = trial.failure_category
        record["required_changes_verified"] = _verified_changes(record)
        record["scenario_passed"] = _scenario_passed(record)
        records.append(record)
        oracle = record["oracle_after"]
        if oracle is None:
            unresolved.append({"task_id": task.id, "boundary": "EVALUATION_RESULT_CAPTURE",
                               "detail": "Terminal workspace was not observable"})
        elif record["harness_result"]["success"] and not oracle["goal_satisfied"]:
            gap = f"False success: {task.id}"
        elif record["validation_result"] is None and "MODEL_FINISHED" in record["runtime_statuses"] and record["harness_result"]["error"] is None:
            gap = f"Model FINISH skipped final validation: {task.id}"
        elif task.id != "true_success" and record["validation_result"] is not None and record["validation_result"]["status"] == "PASS":
            gap = f"Required incorrect behavior accepted: {task.id}"
        if gap or unresolved:
            break
    false_successes = sum(r["harness_result"]["success"] and r["oracle_after"] is not None
                          and not r["oracle_after"]["goal_satisfied"] for r in records)
    completed = len(records) == 3
    passed = completed and all(record["scenario_passed"] for record in records)
    if not gap and not unresolved:
        unresolved.extend({"task_id": record["task_id"], "boundary": record["first_failing_boundary"],
                           "detail": "Observed outcome did not demonstrate the required capability"}
                          for record in records if not record["scenario_passed"])
    return {"result": "BLOCKED" if gap else "PASS" if passed else "PARTIAL",
            "timestamp": datetime.now(timezone.utc).isoformat(), "tasks": records,
            "fixture_self_validation": "PASS" if completed and all(r["self_validation"]["passed"] for r in records) else "FAIL",
            "false_success_count": false_successes, "harness_invariant_gap": gap,
            "unresolved_gaps": unresolved}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = run_core_capability_evaluation()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print(json.dumps({"result": report["result"], "false_success_count": report["false_success_count"],
                      "harness_invariant_gap": report["harness_invariant_gap"]}))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
