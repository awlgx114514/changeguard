"""Isolated execution of fixed evaluation tasks."""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.harness import HarnessOrchestrator

from .dataset import EvaluationDataset, validate_snapshot
from .metrics import calculate_metrics, summarize_trace
from .profiles import RuntimeProfile
from .report import EvaluationReport, TrialResult
from .task import EvaluationTask


@dataclass
class _EvaluationObserver:
    """Immediately reduce Harness observations to privacy-safe scalar evidence."""

    context_size: Any = None
    memory_hits: Any = None
    strategy_hits: Any = None
    terminal_status: Any = None
    terminal_error_count: Any = None
    terminal_open_issue_count: Any = None

    def on_context(self, context: Any) -> None:
        payload = context.to_dict()
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.context_size = len(encoded)
        self.memory_hits = len(payload.get("memory_context", ()))
        self.strategy_hits = len(payload.get("strategies", ()))

    def on_runtime_state(self, state: Any) -> None:
        payload = state.to_dict()
        errors = payload.get("errors", ())
        issues = payload.get("issues", ())
        self.terminal_status = str(payload.get("status", ""))
        self.terminal_error_count = _sequence_length(errors)
        self.terminal_open_issue_count = sum(
            1
            for issue in issues
            if isinstance(issue, Mapping) and str(issue.get("status", "")).upper() == "OPEN"
        ) if _is_sequence(issues) else 0


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _sequence_length(value: Any) -> int:
    return len(value) if _is_sequence(value) else 0


def _validation_failure_category(validation_result: Any) -> str:
    codes = {
        getattr(finding, "code", "")
        for finding in getattr(validation_result, "findings", ())
    }
    if "verification_execution_failed" in codes:
        return "VERIFICATION_EXECUTION"
    if "verification_failed" in codes:
        return "VERIFICATION"
    if codes.intersection({"ASSERTION_FAILED", "ASSERTION_UNSUPPORTED"}):
        return "ASSERTION"
    return "VALIDATION"


class BenchmarkRunner:
    """Run benchmark tasks in disposable, independently committed repositories."""

    def __init__(
        self,
        dataset: EvaluationDataset,
        coding_agent_factory=None,
        harness_factory=HarnessOrchestrator,
    ):
        self.dataset = dataset
        self._coding_agent_factory = coding_agent_factory
        self._harness_factory = harness_factory

    def run_task(self, task: EvaluationTask, mode: str) -> TrialResult:
        profile = RuntimeProfile.for_mode(mode)
        snapshot = self._resolve_snapshot(task)
        fixture_id = self._fixture_id(snapshot)

        with tempfile.TemporaryDirectory(prefix="changeguard-eval-") as temporary:
            repository = Path(temporary) / "repository"
            shutil.copytree(snapshot, repository)
            baseline_commit = self._initialize_repository(repository)
            task_file = Path(temporary) / "task.md"
            task_file.write_text(task.description, encoding="utf-8")

            observer = _EvaluationObserver()
            harness_kwargs = {
                "contract_agent": ContractAgent(
                    generator=lambda task_text, context: task.to_contract()
                ),
                "tool_registry": profile.build_tool_registry(),
                "runtime_loop": True,
                "history_manager_factory": profile.history_manager,
                "trace_manager_factory": profile.trace_manager,
                "action_validator_factory": profile.action_validator,
                "runtime_repair": profile.runtime_repair,
                "runtime_issue_lifecycle": profile.runtime_issue_lifecycle,
                "observer": observer,
            }
            if self._coding_agent_factory is not None:
                harness_kwargs["coding_agent"] = self._coding_agent_factory()

            harness_result = None
            failure_category = None
            data_gaps = []
            try:
                harness_result = self._harness_factory(**harness_kwargs).run(
                    task_file, repository
                )
            except Exception:
                failure_category = "harness_exception"

            repairs = None
            if harness_result is not None:
                try:
                    repairs = self._validated_repairs(harness_result.repairs)
                except (TypeError, ValueError):
                    data_gaps.append("repairs_invalid")

            try:
                trace_events = self._load_trace_events(repository)
            except _TraceEvidenceError as error:
                trace_events = None
                data_gaps.append(error.category)

            validation_evidence = (
                None if harness_result is None else getattr(harness_result, "validation_evidence", None)
            )
            validator_result = (
                None if harness_result is None else getattr(harness_result, "validator_result", None)
            )
            if harness_result is not None and harness_result.error is not None:
                failure = getattr(harness_result, "failure", None)
                failure_category = str(getattr(failure, "stage", "") or "INFRASTRUCTURE")
                validation_evidence = None
                validator_result = None
            elif harness_result is not None and (
                validation_evidence is None or validator_result is None
            ):
                failure_category = "validation_evidence_missing"

            if observer.context_size is None:
                data_gaps.append("context_missing")
            if observer.terminal_status is None:
                data_gaps.append("terminal_state_missing")

            return self._trial_from_evidence(
                task.id,
                profile.mode,
                observer,
                trace_events,
                validation_evidence,
                validator_result,
                repairs,
                fixture_id=fixture_id,
                baseline_commit=baseline_commit,
                failure_category=failure_category,
                data_gaps=tuple(sorted(set(data_gaps))),
            )

    def run(self, mode: str) -> EvaluationReport:
        RuntimeProfile.for_mode(mode)
        started_at = datetime.now(timezone.utc).isoformat()
        return EvaluationReport.from_trials(
            mode,
            tuple(self.run_task(task, mode) for task in self.dataset.tasks),
            dataset_id=self.dataset.id,
            run_started_at=started_at,
            agent_metadata=self._agent_metadata(),
        )

    def _resolve_snapshot(self, task: EvaluationTask) -> Path:
        return validate_snapshot(task, self.dataset.dataset_root)

    @staticmethod
    def _initialize_repository(repository: Path) -> str:
        hooks = repository.parent / "empty-git-hooks"
        hooks.mkdir()
        environment = os.environ.copy()
        for key in tuple(environment):
            if (
                key in {"GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"}
                or key.startswith("GIT_CONFIG_KEY_")
                or key.startswith("GIT_CONFIG_VALUE_")
            ):
                environment.pop(key, None)
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_TEMPLATE_DIR",
        ):
            environment.pop(key, None)
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "ChangeGuard Benchmark",
                "GIT_AUTHOR_EMAIL": "benchmark@example.invalid",
                "GIT_COMMITTER_NAME": "ChangeGuard Benchmark",
                "GIT_COMMITTER_EMAIL": "benchmark@example.invalid",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
            }
        )
        config = (
            "-c", f"core.hooksPath={hooks}",
            "-c", "commit.gpgSign=false",
            "-c", "tag.gpgSign=false",
            "-c", "core.autocrlf=false",
            "-c", "core.eol=lf",
        )

        def run_git(*arguments: str) -> str:
            return subprocess.run(
                ("git", *config, *arguments),
                cwd=repository,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        run_git(
            "init",
            "--quiet",
            "--initial-branch=main",
            "--object-format=sha1",
            "--template=",
        )
        run_git("config", "--local", "user.email", "benchmark@example.invalid")
        run_git("config", "--local", "user.name", "ChangeGuard Benchmark")
        run_git("config", "--local", "core.autocrlf", "false")
        run_git("config", "--local", "core.hooksPath", str(hooks))
        run_git("config", "--local", "commit.gpgSign", "false")
        run_git("add", "--all")
        run_git(
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "evaluation snapshot",
        )
        return run_git("rev-parse", "HEAD")

    @staticmethod
    def _load_trace_events(repository: Path) -> tuple[dict, ...]:
        paths = tuple((repository / ".agent" / "runs").glob("*/trace.json"))
        if not paths:
            raise _TraceEvidenceError("trace_missing")
        if len(paths) != 1:
            raise _TraceEvidenceError("trace_ambiguous")
        try:
            payload = json.loads(paths[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise _TraceEvidenceError("trace_malformed") from error
        if not isinstance(payload, Mapping) or not isinstance(payload.get("events"), list):
            raise _TraceEvidenceError("trace_malformed")
        normalized = []
        for event in payload["events"]:
            if not isinstance(event, Mapping) or not isinstance(event.get("type"), str):
                raise _TraceEvidenceError("trace_malformed")
            data = event.get("data", {})
            if not isinstance(data, Mapping):
                raise _TraceEvidenceError("trace_malformed")
            normalized.append(
                {
                    "type": event["type"],
                    "data": {
                        key: data[key]
                        for key in ("operation", "target")
                        if isinstance(data.get(key), str)
                    },
                }
            )
        return tuple(normalized)

    @staticmethod
    def _validated_repairs(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("Harness repairs must be an integer")
        if value < 0:
            raise ValueError("Harness repairs must not be negative")
        return value

    @staticmethod
    def _trial_from_evidence(
        task_id,
        mode,
        observer,
        trace_events,
        validation_evidence,
        validator_result,
        repairs,
        *,
        fixture_id,
        baseline_commit,
        failure_category,
        data_gaps,
    ) -> TrialResult:
        success = (
            failure_category is None
            and str(getattr(validator_result, "status", "")).upper() == "PASS"
        )
        if validator_result is not None and not success and failure_category is None:
            failure_category = _validation_failure_category(validator_result)
        metrics = calculate_metrics(
            success=success,
            trace_events=trace_events,
            context_bytes=observer.context_size,
            memory_hits=observer.memory_hits,
            strategy_hits=observer.strategy_hits,
            repairs=repairs,
        )
        change_context = getattr(validation_evidence, "change_context", None)
        verification = getattr(validation_evidence, "verification", ())
        return TrialResult(
            task_id=task_id,
            runtime_mode=mode,
            success=success,
            metrics=metrics,
            trace_summary=(
                None if trace_events is None else summarize_trace(trace_events)
            ),
            report_status=(
                "ERROR" if validator_result is None else str(getattr(validator_result, "status", ""))
            ),
            fixture_id=fixture_id,
            baseline_commit=baseline_commit,
            failure_category=failure_category,
            data_gaps=data_gaps,
            finding_count=(
                None if validator_result is None else _sequence_length(getattr(validator_result, "findings", ()))
            ),
            changed_file_count=(
                None if change_context is None else _sequence_length(getattr(change_context, "changed_files", ()))
            ),
            additions=None if change_context is None else int(getattr(change_context, "additions", 0)),
            deletions=None if change_context is None else int(getattr(change_context, "deletions", 0)),
            verification_command_count=(
                None if validation_evidence is None else _sequence_length(verification)
            ),
            verification_all_passed=(
                (all(item.passed for item in verification) if validation_evidence is not None else None)
            ),
            terminal_status=observer.terminal_status,
            terminal_error_count=observer.terminal_error_count,
            terminal_open_issue_count=observer.terminal_open_issue_count,
            assertion_results=(
                ()
                if validator_result is None
                else tuple(
                    {
                        "assertion_id": item.assertion_id,
                        "change_id": item.change_id,
                        "assertion_type": item.assertion_type.value,
                        "status": item.status.value,
                        "evidence_refs": list(item.evidence_refs),
                    }
                    for item in getattr(validator_result, "assertion_results", ())
                )
            ),
        )

    @staticmethod
    def _fixture_id(snapshot: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(item for item in snapshot.rglob("*") if item.is_file()):
            digest.update(path.relative_to(snapshot).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    def _agent_metadata(self) -> Mapping[str, Any]:
        factory = self._coding_agent_factory
        if factory is None:
            agent_type = "CodexAgent"
        else:
            agent_type = getattr(factory, "__name__", type(factory).__name__)
        model = getattr(factory, "model", None) if factory is not None else None
        return {
            "agent_type": str(agent_type),
            "model": model if isinstance(model, str) and model else None,
        }


class _TraceEvidenceError(ValueError):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category
