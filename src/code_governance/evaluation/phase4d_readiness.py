"""Minimal deterministic and optional real-model readiness for Phase 4D.

Evaluation owns pair/trial identity, persistence, external scoring and sample
classification. Harness remains the sole owner of Actions, Governance, repair,
Verification and Validation.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter
from typing import Any, Mapping, Sequence
import uuid

import yaml

from code_governance.agent.codex_agent import CodexAgent
from code_governance.agent.contract_agent import ContractAgent
from code_governance.agent.harness import HarnessOrchestrator
from code_governance.contract.schema import ChangeContract
from code_governance.evaluation.phase4d_checkpoint import (
    CheckpointError,
    FailureCheckpoint,
    _tracked_content_fingerprint,
    digest_json,
    workspace_fingerprint,
    workspace_manifest,
)
from code_governance.evaluation.real_model_repair import (
    FIXTURE_ROOT,
    _ApprovalBoundWorkspaceManager,
    _ScopedFixtureApproval,
    real_model_repair_tasks,
)
from code_governance.evaluation.runner import BenchmarkRunner
from code_governance.runtime.action import Action, OperationKind
from code_governance.runtime.state import AgentState
from code_governance.tools import ApprovalOutcome
from code_governance.trace.writer import TraceManager
from code_governance.validator.repair_context import repair_failure_identity
from code_governance.workspace.manager import WorkspaceManager


CLASSIFIER_VERSION = "sample-validity-v1"
TRIAL_STATES = (
    "PLANNED", "STARTED", "PROVIDER_DECISION_STARTED", "RUNNING",
    "HARNESS_TERMINATED", "FINAL_STATE_EXPORTED", "ORACLE_COMPLETED", "CLASSIFIED",
)
_REPAIR_START_MARKER = "Validation Repair Evidence:\n"
_REPAIR_END_MARKER = "Context Constraints:\n"
_MODEL_INPUT_PROVENANCE_GUIDANCE = (
    "Source classifications for the model input below:\n"
    "- PUBLIC_INPUT: the User task and Change Contract.\n"
    "- VERIFIED_RUNTIME_FACT: Harness status, action/observation counts, canonical tool results, and Validation Repair Evidence with validated provenance.\n"
    "- UNVERIFIED_RUNTIME_SUMMARY: Context summaries, Working Memory, Active Issues, free-form errors, and Constraints; summarizing or repeating a claim does not verify it.\n"
    "- MODEL_GENERATED_UNVERIFIED: Recent Decisions, Action reasons, and other model-written text remain unverified even when retained in history.\n\n"
)
_ALLOWED_PROVENANCE = {
    "task", "contract", "execution_result", "verification_evidence",
    "assertion_result", "validation_result", "validation_evidence", "changed_files",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _explicit_external_provider_status(detail: str) -> str:
    """Return a category only when provider diagnostics explicitly name it."""
    lowered = str(detail).lower()
    if any(token in lowered for token in ("rate limit", "too many requests", "http 429", "status 429")):
        return "rate_limited"
    if any(token in lowered for token in (
        "http 503", "status 503", "service unavailable", "provider unavailable",
    )):
        return "service_failure"
    if any(token in lowered for token in (
        "provider transport outage", "upstream transport outage", "api transport outage",
    )):
        return "transport_outage"
    return ""


def _explicit_provider_status_from_streams(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    stderr_text = (
        stderr.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes) else str(stderr or "")
    )
    status = _explicit_external_provider_status(stderr_text)
    if status:
        return status
    stdout_text = (
        stdout.decode("utf-8", errors="replace")
        if isinstance(stdout, bytes) else str(stdout or "")
    )
    for line in stdout_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") not in {"error", "response.failed"}:
            continue
        candidates = [event.get("message"), event.get("error")]
        if isinstance(event.get("error"), dict):
            candidates.extend((event["error"].get("message"), event["error"].get("status")))
        status = _explicit_external_provider_status(" ".join(str(item) for item in candidates if item is not None))
        if status:
            return status
    return ""


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


_PROVIDER_BOUNDARY_KEYS = {
    "decision_started": "decision_started",
    "process_started": "process_started",
    "communicate_started": "communicate_started",
    "stdin_completed": "stdin_completed",
    "input_accepted": "input_accepted",
    "first_stdout_observed": "first_stdout_observed",
    "first_stderr_observed": "first_stderr_observed",
    "assistant_output_observed": "assistant_output_observed",
    "action_parse_started": "action_parse_started",
    "action_parsed": "action_parsed",
    "deadline_fired": "deadline_fired",
    "termination_started": "termination_started",
    "process_exited": "process_exited",
    "cleanup_output_observed": "cleanup_output_observed",
    "pipe_eof_stdout": "pipe_eof_stdout",
    "pipe_eof_stderr": "pipe_eof_stderr",
    "termination_completed": "termination_completed",
    "termination_failed": "termination_failed",
    "cleanup_failed": "cleanup_failed",
    "cleanup_completed": "cleanup_completed",
}


def _actual_provider_identity(
    command: Sequence[str], *, cwd: str | Path | None = None,
    workspace_root: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Describe the executable and CLI package that this invocation will launch."""
    arguments = [str(item) for item in command]
    environment = os.environ if environment is None else environment
    if not arguments:
        raise ValueError("provider command must not be empty")
    executable = arguments[0]
    resolved_executable = shutil.which(executable)
    if resolved_executable is None:
        candidate = Path(executable)
        resolved_executable = str(candidate.resolve()) if candidate.is_absolute() or candidate.exists() else executable
    else:
        resolved_executable = str(Path(resolved_executable).resolve())

    entrypoint = None
    for argument in arguments[1:]:
        candidate = Path(argument)
        if candidate.name.lower() in {"codex.js", "codex.cjs"}:
            try:
                if candidate.is_file():
                    entrypoint = candidate.resolve()
                    break
            except OSError:
                pass

    cli_version = None
    version_source = None
    if entrypoint is not None:
        for parent in entrypoint.parents:
            if parent.name != "codex" or parent.parent.name != "@openai":
                continue
            package_path = parent / "package.json"
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                break
            if isinstance(package, dict) and isinstance(package.get("version"), str):
                cli_version = package["version"]
                version_source = "package_json_at_invoked_entrypoint"
            break

    config_home = Path(environment.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    config_path = config_home / "config.toml"
    config_state = "missing"
    config_sha256 = None
    config_values: dict[str, Any] = {}
    try:
        raw_config = config_path.read_bytes()
        config_sha256 = _sha(raw_config)
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        config_values = tomllib.loads(raw_config.decode("utf-8"))
        config_state = "loaded"
    except FileNotFoundError:
        pass
    except Exception:
        config_state = "unreadable_or_unparseable"
    model, reasoning = _explicit_cli_model_configuration(arguments)
    overrides, profile = _explicit_cli_config_overrides(arguments)
    selected_profile = profile or config_values.get("profile")
    if selected_profile and isinstance(config_values.get("profiles"), Mapping):
        profile_values = config_values["profiles"].get(selected_profile, {})
        if isinstance(profile_values, Mapping):
            config_values = {**config_values, **profile_values}
    model = model or overrides.get("model") or config_values.get("model")
    reasoning = reasoning or {
        "model_reasoning_effort": config_values.get("model_reasoning_effort")
    } if config_values.get("model_reasoning_effort") else reasoning
    for key, value in overrides.items():
        if key == "model_reasoning_effort":
            reasoning = {**(reasoning or {}), key: value}

    relevant_environment = {}
    for key in (
        "CODEX_HOME", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID", "AZURE_OPENAI_ENDPOINT", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    ):
        if key in environment:
            relevant_environment[key] = str(environment[key])
    for key in ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY"):
        if key in environment:
            relevant_environment[key] = {
                "present": bool(environment[key]),
                "sha256": _sha(str(environment[key])) if environment[key] else "",
            }
    semantic_argv = []
    skip_next = False
    for index, argument in enumerate(arguments):
        if skip_next:
            skip_next = False
            continue
        if argument in {"--output-schema", "--output-last-message"} and index + 1 < len(arguments):
            semantic_argv.extend([argument, "<schema-file>" if argument == "--output-schema" else "<output-file>"])
            skip_next = True
            continue
        if Path(argument).name.lower() in {"codex.js", "codex.cjs"}:
            semantic_argv.append("<codex-cli-entrypoint>")
        elif index == 0 and Path(argument).name.lower().startswith("node"):
            semantic_argv.append("<node-runtime>")
        elif index == 0:
            semantic_argv.append("<provider-executable>")
        else:
            semantic_argv.append(argument)
    cwd_path = Path(cwd).resolve() if cwd is not None else None
    workspace_path = Path(workspace_root).resolve() if workspace_root is not None else None
    cwd_relative_path = None
    cwd_role = None
    if cwd_path is not None and workspace_path is not None:
        try:
            relative = cwd_path.relative_to(workspace_path)
        except ValueError:
            cwd_role = "outside_workspace"
        else:
            cwd_relative_path = relative.as_posix() or "."
            cwd_role = "workspace_root" if cwd_relative_path == "." else "workspace_subdirectory"
    cli_config_effective = bool(config_state == "loaded" or (model is not None and reasoning is not None))
    sampling_verifiable = bool(model and reasoning and cli_config_effective)
    return {
        "actual_executable": executable,
        "actual_executable_path": resolved_executable,
        "launch_path": str(entrypoint or resolved_executable),
        "launch_argv": arguments,
        "semantic_launch_argv": semantic_argv,
        "cwd_role": cwd_role,
        "cwd_relative_path": cwd_relative_path,
        "cwd_absolute_sha256": None if cwd_path is None else _sha(str(cwd_path)),
        "provider_environment_fingerprint": digest_json(relevant_environment),
        "provider_environment_keys": sorted(relevant_environment),
        "session_mode": "resume" if any(item in {"resume", "--resume", "--session"} for item in arguments) else "new_session",
        "config_source": str(config_path.resolve()) if config_state == "loaded" else str(config_path),
        "config_state": config_state,
        "config_sha256": config_sha256,
        "selected_profile": selected_profile,
        "actual_cli_version": cli_version,
        "actual_cli_version_source": version_source,
        "effective_model": model,
        "effective_model_source": (
            "explicit_cli_argument" if _explicit_cli_model_configuration(arguments)[0] is not None
            else "cli_config" if model is not None else "unknown"
        ),
        "effective_reasoning_configuration": reasoning,
        "effective_reasoning_source": (
            "explicit_cli_argument" if _explicit_cli_model_configuration(arguments)[1] is not None
            else "cli_config" if reasoning is not None else "unknown"
        ),
        "sampling_identity_verifiable": sampling_verifiable,
    }


def _explicit_cli_model_configuration(arguments: Sequence[str]) -> tuple[str | None, dict[str, str] | None]:
    model = None
    reasoning: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        item = str(arguments[index])
        if item in {"--model", "-m"} and index + 1 < len(arguments):
            model = str(arguments[index + 1])
            index += 2
            continue
        if item == "--reasoning-effort" and index + 1 < len(arguments):
            reasoning["model_reasoning_effort"] = str(arguments[index + 1])
            index += 2
            continue
        if item in {"-c", "--config"} and index + 1 < len(arguments):
            setting = str(arguments[index + 1])
            key, separator, value = setting.partition("=")
            if separator:
                value = value.strip().strip("\"'")
                if key == "model":
                    model = value
                elif key in {"model_reasoning_effort", "reasoning_effort"}:
                    reasoning[key] = value
            index += 2
            continue
        index += 1
    return model, reasoning or None


def _explicit_cli_config_overrides(arguments: Sequence[str]) -> tuple[dict[str, str], str | None]:
    overrides: dict[str, str] = {}
    profile = None
    index = 0
    while index < len(arguments):
        item = str(arguments[index])
        if item in {"-c", "--config"} and index + 1 < len(arguments):
            key, separator, value = str(arguments[index + 1]).partition("=")
            if separator and key in {"model", "model_reasoning_effort"}:
                overrides[key] = value.strip().strip("\"'")
            index += 2
            continue
        if item == "--profile" and index + 1 < len(arguments):
            profile = str(arguments[index + 1])
            index += 2
            continue
        index += 1
    return overrides, profile


def _transport_fallback_observation(*streams: str | bytes) -> bool | None:
    text_parts = [
        item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        for item in streams if item
    ]
    if not text_parts:
        return None
    text = "\n".join(text_parts).lower()
    if ("fallback" in text or "falling back" in text) and "websocket" in text and ("https" in text or "http" in text):
        return True
    return None


def _write_diagnostic_artifact(
    directory: Path, provider_call_local_id: str, stream_name: str,
    content: str | bytes, *, max_bytes: int = 64 * 1024,
) -> dict[str, Any]:
    raw = content.encode("utf-8", errors="replace") if isinstance(content, str) else bytes(content)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", provider_call_local_id)
    safe_stream = re.sub(r"[^A-Za-z0-9_-]", "_", stream_name)
    relative = Path("provider-artifacts") / f"{safe_id}.{safe_stream}.log"
    path = Path(directory) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(raw) <= max_bytes:
        stored = raw
    else:
        marker = f"\n...[truncated; sha256={_sha(raw)}; original_bytes={len(raw)}]...\n".encode("ascii")
        if max_bytes <= len(marker):
            stored = raw[:max_bytes]
        else:
            content_budget = max_bytes - len(marker)
            head_size = content_budget // 2
            stored = raw[:head_size] + marker + raw[-(content_budget - head_size):]
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as stream:
        stream.write(stored)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return {
        "path": relative.as_posix(),
        "sha256": _sha(raw),
        "original_bytes": len(raw),
        "stored_bytes": len(stored),
        "truncated": len(stored) < len(raw),
    }


class TrialRecord:
    """Atomically persisted complete snapshots; not an event-sourcing store."""

    def __init__(self, path: Path, payload: dict[str, Any]):
        self.path = Path(path)
        self.payload = payload

    @classmethod
    def create(
        cls, path: Path, *, experiment_id: str, pair_id: str, checkpoint_id: str,
        trial_id: str, run_id: str, condition: str, task_id: str = "",
    ) -> "TrialRecord":
        if condition not in {"prefix", "baseline", "treatment"}:
            raise ValueError("condition must be prefix, baseline, or treatment")
        payload = {
            "schema_version": 1,
            "experiment_id": experiment_id,
            "pair_id": pair_id,
            "checkpoint_id": checkpoint_id,
            "trial_id": trial_id,
            "run_id": run_id,
            "task_id": task_id,
            "condition": condition,
            "status": "PLANNED",
            "created_at": _now(),
            "updated_at": _now(),
            "events": [{"status": "PLANNED", "at": _now()}],
            "provider_calls": [],
            "evidence_lineage": [],
            "setup_checks": {},
        }
        record = cls(Path(path), payload)
        record._write()
        return record

    @classmethod
    def load(cls, path: Path) -> "TrialRecord":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"unable to read TrialRecord: {error}") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("unsupported TrialRecord schema")
        return cls(Path(path), payload)

    def _write(self) -> None:
        self.payload["updated_at"] = _now()
        _atomic_json(self.path, self.payload)

    def transition(self, status: str, **facts: Any) -> None:
        if status not in TRIAL_STATES:
            raise ValueError(f"unsupported trial state: {status}")
        self.payload["status"] = status
        self.payload["events"].append({"status": status, "at": _now(), **facts})
        self._write()

    def update(self, **facts: Any) -> None:
        self.payload.update(facts)
        self._write()

    def provider_decision_started(
        self, *, decision_seq: int, provider_call_local_id: str,
        workspace_id: str = "", context_projection_sha256: str = "",
        model_input_sha256: str = "", input_without_repair_sha256: str = "",
        decision_schema_sha256: str = "", provider_configuration_fingerprint: str = "",
        repair_provenance_valid: bool = False,
        actual_executable: str | None = None,
        actual_executable_path: str | None = None,
        launch_path: str | None = None,
        launch_argv: Sequence[str] | None = None,
        actual_cli_version: str | None = None,
        actual_cli_version_source: str | None = None,
        semantic_launch_argv: Sequence[str] | None = None,
        cwd_role: str | None = None,
        cwd_relative_path: str | None = None,
        cwd_absolute_sha256: str | None = None,
        provider_environment_fingerprint: str | None = None,
        provider_environment_keys: Sequence[str] | None = None,
        session_mode: str | None = None,
        config_state: str | None = None,
        selected_profile: str | None = None,
        sampling_identity_verifiable: bool | None = None,
        effective_model: str | None = None,
        effective_model_source: str | None = None,
        effective_reasoning_configuration: Mapping[str, str] | None = None,
        effective_reasoning_source: str | None = None,
        config_source: str | None = None,
        config_sha256: str | None = None,
        invocation_fingerprint: str | None = None,
        cwd: str | None = None,
        prompt_sha256: str | None = None,
        schema_sha256: str | None = None,
        preflight_cli_version: str | None = None,
        preflight_launch_path: str | None = None,
        preflight_model: str | None = None,
        preflight_reasoning_configuration: Mapping[str, str] | None = None,
        preflight_config_source: str | None = None,
        preflight_config_sha256: str | None = None,
    ) -> None:
        started_at = _now()
        boundaries = {key: None for key in _PROVIDER_BOUNDARY_KEYS}
        boundaries["decision_started"] = started_at
        prompt_hash = prompt_sha256 or model_input_sha256 or None
        schema_hash = schema_sha256 or decision_schema_sha256 or None
        launch = str(launch_path) if launch_path is not None else None
        preflight_matches = {
            "version": None if preflight_cli_version is None or actual_cli_version is None
            else preflight_cli_version == actual_cli_version,
            "launch_path": None if preflight_launch_path is None or launch is None
            else preflight_launch_path == launch,
            "model": None if preflight_model is None or effective_model is None
            else preflight_model == effective_model,
            "reasoning_configuration": None
            if preflight_reasoning_configuration is None or effective_reasoning_configuration is None
            else dict(preflight_reasoning_configuration) == dict(effective_reasoning_configuration),
            "config_source": None if preflight_config_source is None or config_source is None
            else preflight_config_source == config_source,
            "config_sha256": None if preflight_config_sha256 is None or config_sha256 is None
            else preflight_config_sha256 == config_sha256,
        }
        known_matches = list(preflight_matches.values())
        preflight_identity_match = (
            False if any(value is False for value in known_matches)
            else None if not known_matches or any(value is None for value in known_matches)
            else True
        )
        call = {
            "trial_id": self.payload["trial_id"],
            "run_id": self.payload["run_id"],
            "decision_seq": int(decision_seq),
            "provider_call_local_id": provider_call_local_id,
            "workspace_id": workspace_id,
            "checkpoint_id": self.payload["checkpoint_id"],
            "started_at": started_at,
            "start_timestamp": started_at,
            "last_known_boundary": "decision_started",
            "boundary_timestamps": boundaries,
            "native_tool_call_count": None,
            "context_projection_sha256": context_projection_sha256,
            "model_input_sha256": model_input_sha256,
            "input_without_repair_sha256": input_without_repair_sha256,
            "decision_schema_sha256": decision_schema_sha256,
            "prompt_sha256": prompt_hash,
            "schema_sha256": schema_hash,
            "provider_configuration_fingerprint": provider_configuration_fingerprint,
            "repair_provenance_valid": bool(repair_provenance_valid),
            "actual_executable": actual_executable,
            "actual_executable_path": actual_executable_path,
            "launch_path": launch,
            "launch_argv": list(launch_argv) if launch_argv is not None else None,
            "actual_cli_version": actual_cli_version,
            "actual_cli_version_source": actual_cli_version_source,
            "semantic_launch_argv": list(semantic_launch_argv) if semantic_launch_argv is not None else None,
            "cwd_role": cwd_role,
            "cwd_relative_path": cwd_relative_path,
            "cwd_absolute_sha256": cwd_absolute_sha256,
            "provider_environment_fingerprint": provider_environment_fingerprint,
            "provider_environment_keys": list(provider_environment_keys) if provider_environment_keys is not None else None,
            "session_mode": session_mode,
            "config_state": config_state,
            "selected_profile": selected_profile,
            "sampling_identity_verifiable": sampling_identity_verifiable,
            "effective_model": effective_model,
            "effective_model_source": effective_model_source,
            "effective_reasoning_configuration": (
                dict(effective_reasoning_configuration)
                if effective_reasoning_configuration is not None else None
            ),
            "effective_reasoning_source": effective_reasoning_source,
            "config_source": config_source,
            "config_sha256": config_sha256,
            "invocation_fingerprint": invocation_fingerprint,
            "cwd": cwd,
            "preflight_cli_version": preflight_cli_version,
            "preflight_launch_path": preflight_launch_path,
            "preflight_model": preflight_model,
            "preflight_reasoning_configuration": (
                dict(preflight_reasoning_configuration)
                if preflight_reasoning_configuration is not None else None
            ),
            "preflight_config_source": preflight_config_source,
            "preflight_config_sha256": preflight_config_sha256,
            "preflight_identity_matches_trial": preflight_identity_match,
            "preflight_identity_comparison": preflight_matches,
            "preflight_actual_version_match": preflight_matches["version"],
        }
        self.payload["provider_calls"].append(call)
        self.transition(
            "PROVIDER_DECISION_STARTED",
            provider_call_local_id=provider_call_local_id,
            decision_seq=decision_seq,
        )

    def provider_boundary(self, provider_call_local_id: str, boundary: str, **facts: Any) -> None:
        call = self._provider_call(provider_call_local_id)
        key = _PROVIDER_BOUNDARY_KEYS.get(boundary)
        if key is not None:
            timestamps = call["boundary_timestamps"]
            if timestamps.get(key) is None:
                timestamps[key] = _now()
        if boundary == "assistant_output_artifact_checked":
            present = facts.get("artifact_present")
            if isinstance(present, bool):
                call["assistant_output_artifact_present"] = present
                if present and call["boundary_timestamps"]["assistant_output_observed"] is None:
                    call["boundary_timestamps"]["assistant_output_observed"] = _now()
            if facts.get("artifact_sha256"):
                call["assistant_output_sha256"] = str(facts["artifact_sha256"])
            if facts.get("artifact_size_bytes") is not None:
                call["assistant_output_bytes"] = int(facts["artifact_size_bytes"])
        if boundary == "action_parsed":
            if facts.get("action_id") is not None:
                call["canonical_action_id"] = str(facts["action_id"])
            if facts.get("operation") is not None:
                call["canonical_operation"] = str(facts["operation"])
        if boundary == "process_started" and facts.get("pid") is not None:
            call["process_id"] = int(facts["pid"])
        if boundary == "process_exited" and facts.get("returncode") is not None:
            call["process_returncode"] = int(facts["returncode"])
            if call["boundary_timestamps"].get("deadline_fired") is not None:
                call.setdefault("cleanup_facts", {})["returncode"] = int(facts["returncode"])
        if boundary == "deadline_fired" and facts.get("timeout_seconds") is not None:
            call["timeout_seconds"] = float(facts["timeout_seconds"])
        if boundary in {"deadline_fired", "cleanup_output_observed"}:
            field = "deadline_snapshot" if boundary == "deadline_fired" else "cleanup_facts"
            snapshot = call.setdefault(field, {})
            for stream_name in ("stdout", "stderr"):
                value = facts.get(stream_name)
                if value is None:
                    continue
                raw = value.encode("utf-8", errors="replace") if isinstance(value, str) else bytes(value)
                snapshot[f"{stream_name}_sha256"] = _sha(raw)
                snapshot[f"{stream_name}_bytes"] = len(raw)
                reference = _write_diagnostic_artifact(
                    self.path.parent,
                    provider_call_local_id,
                    f"{field.replace('_', '-')}-{stream_name}",
                    raw,
                )
                snapshot[f"{stream_name}_artifact_ref"] = reference
            if "streams_complete" in facts:
                snapshot["streams_complete"] = bool(facts["streams_complete"])
        if boundary == "termination_completed":
            call.setdefault("cleanup_facts", {})["returncode"] = facts.get("returncode")
        if boundary in {"termination_failed", "cleanup_failed"}:
            call.setdefault("cleanup_facts", {})[f"{boundary}_error"] = {
                "error_type": str(facts.get("error_type", "")),
                "detail_sha256": _sha(str(facts.get("detail", ""))[:512]),
            }
        call["last_known_boundary"] = boundary
        self._write()

    def provider_call_updated(self, provider_call_local_id: str, **facts: Any) -> None:
        call = self._provider_call(provider_call_local_id)
        call.update(facts)
        self._write()

    def validation_attempt_observed(self, attempt_id: str, payload: Mapping[str, Any]) -> None:
        self.payload["evidence_lineage"].append({
            "kind": "validation_attempt",
            "validation_attempt_id": str(attempt_id),
            "payload": dict(payload),
            "observed_at": _now(),
        })
        self._write()

    def provider_call_interrupted(
        self, provider_call_local_id: str, *, category: str,
        stdout=None, stderr=None, response_artifact=None,
    ) -> None:
        call = self._provider_call(provider_call_local_id)
        call.update({
            "interruption_category": str(category),
            "interruption_observed_at": _now(),
            "diagnostic_capture_scope": "interrupted_process_partial",
        })
        for name, value in (("stdout", stdout), ("stderr", stderr), ("assistant-output", response_artifact)):
            if value is None:
                continue
            raw = value.encode("utf-8", errors="replace") if isinstance(value, str) else bytes(value)
            call[f"{name.replace('-', '_')}_artifact_ref"] = _write_diagnostic_artifact(
                self.path.parent, provider_call_local_id, name, raw,
            )
            call[f"{name.replace('-', '_')}_sha256"] = _sha(raw)
        if response_artifact:
            call["assistant_output_artifact_present"] = True
            call["boundary_timestamps"]["assistant_output_observed"] = _now()
        call["last_known_boundary"] = "interruption_observed"
        self._write()

    def runtime_trace_boundary(self, event: Mapping[str, Any]) -> None:
        facts = self.payload.setdefault("runtime_facts", {"boundaries": []})
        bounded = {
            "event": str(event.get("event", "")),
            "sequence": int(event.get("sequence", 0)),
            "timestamp": str(event.get("timestamp", "")),
            "data": dict(event.get("data", {})),
        }
        facts["last_confirmed_boundary"] = bounded["event"]
        facts["last_confirmed_at"] = bounded["timestamp"]
        facts["boundaries"].append(bounded)
        self._write()

    def runtime_state_observed(self, state: AgentState) -> None:
        failure = state.failure
        self.payload["runtime_state_snapshot"] = {
            "run_id": state.run_id,
            "status": state.status,
            "step_count": state.step_count,
            "recent_actions": [item.to_dict() for item in state.recent_actions],
            "recent_observations": [item.to_dict() for item in state.recent_observations],
            "working_memory_summary": [item.to_dict() for item in state.working_memory_summary],
            "decisions": list(state.decisions),
            "issues": [item.to_dict() for item in state.issues],
            "workspace_revision": state.workspace_revision,
            "failure_stage": None if failure is None else failure.stage,
            "failure_category": None if failure is None else failure.category,
            "termination_reason": state.termination_reason,
            "validation_result": state.validation_result,
        }
        self._write()

    def provider_process_returned(
        self, provider_call_local_id: str, *, returncode: int,
        response_artifact_present: bool, stdout: str | bytes,
        stderr: str | bytes, response_artifact: str | bytes | None,
        native_tool_call_count: int | None,
        native_call_observation_complete: bool, event_types: Sequence[str],
        duration_seconds: float,
    ) -> None:
        call = self._provider_call(provider_call_local_id)
        stdout_bytes = stdout.encode("utf-8", errors="replace") if isinstance(stdout, str) else bytes(stdout)
        stderr_bytes = stderr.encode("utf-8", errors="replace") if isinstance(stderr, str) else bytes(stderr)
        stdout_ref = _write_diagnostic_artifact(self.path.parent, provider_call_local_id, "stdout", stdout_bytes)
        stderr_ref = _write_diagnostic_artifact(self.path.parent, provider_call_local_id, "stderr", stderr_bytes)
        response_ref = None
        if response_artifact is not None:
            response_ref = _write_diagnostic_artifact(
                self.path.parent, provider_call_local_id, "assistant-output", response_artifact,
            )
        call.update({
            "process_returncode": int(returncode),
            "response_artifact_present": bool(response_artifact_present),
            "assistant_output_artifact_ref": response_ref,
            "process_returned_at": _now(),
            "duration_seconds": float(duration_seconds),
            "stdout_sha256": _sha(stdout_bytes),
            "stderr_sha256": _sha(stderr_bytes),
            "stdout_artifact_ref": stdout_ref,
            "stderr_artifact_ref": stderr_ref,
            "native_tool_call_count": native_tool_call_count,
            "native_call_observation_complete": bool(native_call_observation_complete),
            "provider_event_types": list(event_types),
        })
        combined_output = (stdout_bytes + b"\n" + stderr_bytes).decode("utf-8", errors="replace")
        provider_status = _explicit_provider_status_from_streams(stdout_bytes, stderr_bytes)
        if provider_status:
            call["provider_status"] = provider_status
        fallback_observed = _transport_fallback_observation(stdout_bytes, stderr_bytes)
        if fallback_observed is not None:
            call["transport_fallback_observed"] = fallback_observed
        call["provider_retry_message_count"] = sum(
            1 for line in combined_output.splitlines()
            if re.search(r"\b(retry|retrying|attempt\s+\d+)\b", line, re.IGNORECASE)
        )
        self._write()

    def provider_action_received(self, provider_call_local_id: str, action: dict[str, Any]) -> None:
        call = self._provider_call(provider_call_local_id)
        call.update({
            "canonical_action_id": str(action.get("id", "")),
            "canonical_operation": str(
                action.get("operation", {}).get("kind", "")
                if isinstance(action.get("operation"), Mapping)
                else action.get("operation", "")
            ),
        })
        self._write()
        self.transition("RUNNING", provider_call_local_id=provider_call_local_id)

    def provider_call_failed(
        self, provider_call_local_id: str, *, category: str, detail_sha256: str = "",
        duration_seconds: float | None = None, provider_status: str = "",
        stdout: str | bytes | None = None, stderr: str | bytes | None = None,
        response_artifact: str | bytes | None = None,
        capture_scope: str | None = None,
    ) -> None:
        call = self._provider_call(provider_call_local_id)
        call.update({
            "error_category": category[:128],
            "error_detail_sha256": detail_sha256,
            "failed_at": _now(),
        })
        for stream_name, content in (("stdout", stdout), ("stderr", stderr)):
            if content is None:
                continue
            raw = content.encode("utf-8", errors="replace") if isinstance(content, str) else bytes(content)
            call[f"{stream_name}_sha256"] = _sha(raw)
            call[f"{stream_name}_artifact_ref"] = _write_diagnostic_artifact(
                self.path.parent, provider_call_local_id, stream_name, raw,
            )
        if response_artifact is not None:
            call["assistant_output_artifact_ref"] = _write_diagnostic_artifact(
                self.path.parent, provider_call_local_id, "assistant-output", response_artifact,
            )
            call["response_artifact_present"] = True
        stream_status = _explicit_provider_status_from_streams(stdout, stderr)
        if stream_status:
            provider_status = stream_status
        if duration_seconds is not None:
            call["duration_seconds"] = float(duration_seconds)
        if provider_status in {"service_failure", "rate_limited", "transport_outage"}:
            call["provider_status"] = provider_status
        if capture_scope is not None:
            call["diagnostic_capture_scope"] = capture_scope
        fallback_observed = _transport_fallback_observation(
            *(item for item in (stdout, stderr) if item is not None)
        )
        if fallback_observed is not None:
            call["transport_fallback_observed"] = fallback_observed
        self._write()

    def _provider_call(self, provider_call_local_id: str) -> dict[str, Any]:
        for call in reversed(self.payload["provider_calls"]):
            if call["provider_call_local_id"] == provider_call_local_id:
                return call
        raise ValueError(f"unknown provider call id: {provider_call_local_id}")


class SampleValidity(str, Enum):
    VALID = "VALID"
    INVALID_SETUP = "INVALID_SETUP"
    INVALID_EXTERNAL = "INVALID_EXTERNAL"
    UNDETERMINED = "UNDETERMINED"


def classify_sample(facts: Mapping[str, Any]) -> SampleValidity:
    """Classify only from explicit evidence; ambiguous provider timeouts abstain."""
    setup = facts.get("setup_checks", {})
    if isinstance(setup, Mapping) and any(value is False for value in setup.values()):
        return SampleValidity.INVALID_SETUP
    if facts.get("oracle_execution_status") == "ERROR" or facts.get("oracle_wiring_error") is True:
        return SampleValidity.INVALID_SETUP
    if facts.get("evaluation_contract_violation") is True:
        return SampleValidity.INVALID_SETUP

    provider_status = str(facts.get("provider_status", "")).lower()
    explicit_external = {
        "service_failure", "rate_limited", "transport_outage", "provider_unavailable",
        "explicit_external_dependency_outage",
    }
    if provider_status in explicit_external or facts.get("external_outage_confirmed") is True:
        return SampleValidity.INVALID_EXTERNAL

    if facts.get("timeout_observed") is True or facts.get("evidence_complete") is False:
        return SampleValidity.UNDETERMINED

    if (
        isinstance(setup, Mapping)
        and "canonical_action_path" in setup
        and setup["canonical_action_path"] is None
    ):
        return SampleValidity.UNDETERMINED

    model_outcome = str(facts.get("model_outcome", "")).lower()
    valid_failures = {
        "wrong_repair", "early_finish", "policy_violation_rejected",
        "step_budget_exhausted", "repair_budget_exhausted", "normal_intervention_cost",
        "goal_not_satisfied",
    }
    if model_outcome in valid_failures and facts.get("evidence_complete") is True:
        return SampleValidity.VALID
    if facts.get("execution_completed") is True and facts.get("evidence_complete") is True:
        return SampleValidity.VALID
    return SampleValidity.UNDETERMINED


def primary_success(harness_success: bool, oracle_success: bool | None) -> dict[str, Any]:
    primary = None if oracle_success is None else bool(harness_success and oracle_success)
    return {
        "harness_success": bool(harness_success),
        "oracle_success": oracle_success,
        "primary_success": primary,
        "false_success": bool(harness_success and oracle_success is False),
    }


def readiness_summary(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid = [item for item in samples if item.get("validity") == SampleValidity.VALID.value]
    false_success_count = sum(bool(item.get("harness_success") and item.get("oracle_success") is False) for item in samples)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for index, item in enumerate(samples):
        pair_id = str(item.get("pair_id") or f"single:{index}")
        groups.setdefault(pair_id, []).append(item)
    valid_pair_count = sum(
        {str(item.get("condition", "")) for item in pair}
        == {"baseline", "treatment"}
        and all(item.get("validity") == SampleValidity.VALID.value for item in pair)
        for pair in groups.values()
    )
    if not valid_pair_count:
        effect_status = "NOT_ESTIMABLE"
    else:
        effect_status = "READINESS_ONLY"
    path_values = [item.get("canonical_action_path_preserved") for item in samples]
    if any(value is False for value in path_values):
        canonical_status = "FAILED"
    elif samples and all(value is True for value in path_values):
        canonical_status = "PROVEN"
    else:
        canonical_status = "NOT_PROVEN"
    readiness = (
        "FAIL" if false_success_count or canonical_status == "FAILED"
        else "NOT_PROVEN" if canonical_status != "PROVEN" and samples
        else "NOT_ESTIMABLE" if not valid_pair_count
        else "PASS"
    )
    return {
        "valid_pair_count": int(valid_pair_count),
        "valid_trial_count": len(valid),
        "false_success_count": false_success_count,
        "effect_status": effect_status,
        "effect_conclusion": "NOT_RUN",
        "canonical_action_path_status": canonical_status,
        "readiness": readiness,
    }


def _repair_region(prompt: str) -> tuple[str, str, str] | None:
    if prompt.count(_REPAIR_START_MARKER) != 1 or prompt.count(_REPAIR_END_MARKER) != 1:
        return None
    start = prompt.index(_REPAIR_START_MARKER) + len(_REPAIR_START_MARKER)
    end = prompt.index(_REPAIR_END_MARKER, start)
    if end < start:
        return None
    return prompt[:start], prompt[start:end], prompt[end:]


def compare_initial_inputs(baseline_prompt: str, treatment_prompt: str) -> dict[str, Any]:
    baseline = _repair_region(baseline_prompt)
    treatment = _repair_region(treatment_prompt)
    if baseline is None or treatment is None:
        return {"consistent": False, "intervention_only_repair_context": False, "reason": "repair region missing or ambiguous"}
    try:
        baseline_value = yaml.safe_load(baseline[1])
        treatment_value = yaml.safe_load(treatment[1])
    except yaml.YAMLError:
        return {"consistent": False, "intervention_only_repair_context": False, "reason": "repair region is not valid YAML"}
    same_base = baseline[0] == treatment[0] and baseline[2] == treatment[2]
    expected_intervention = baseline_value in (None, []) and isinstance(treatment_value, list) and bool(treatment_value)
    return {
        "consistent": bool(same_base and expected_intervention),
        "intervention_only_repair_context": bool(same_base and expected_intervention),
        "baseline_without_repair_sha256": _sha(baseline[0] + baseline[2]),
        "treatment_without_repair_sha256": _sha(treatment[0] + treatment[2]),
        "baseline_repair_sha256": _sha(baseline[1]),
        "treatment_repair_sha256": _sha(treatment[1]),
        "reason": "" if same_base and expected_intervention else "non-repair prompt content differs or intervention is not treatment-only",
    }


def compare_provider_initial_inputs(
    baseline_calls: Sequence[Mapping[str, Any]],
    treatment_calls: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare the persisted first actual provider inputs for one A/B pair."""
    if not baseline_calls or not treatment_calls:
        return {"consistent": False, "intervention_only_repair_context": False,
                "reason": "a trial has no observed provider decision"}
    baseline = min(baseline_calls, key=lambda item: int(item.get("decision_seq", 0)))
    treatment = min(treatment_calls, key=lambda item: int(item.get("decision_seq", 0)))
    required = (
        "model_input_sha256", "input_without_repair_sha256", "context_projection_sha256",
        "decision_schema_sha256", "checkpoint_id", "provider_configuration_fingerprint",
        "trial_id", "run_id", "workspace_id",
    )
    if any(not baseline.get(key) or not treatment.get(key) for key in required):
        return {"consistent": False, "intervention_only_repair_context": False,
                "reason": "first provider decision lacks required identity/input hashes"}
    actual_identity_fields = (
        "actual_cli_version", "actual_executable_path", "launch_path", "effective_model",
        "effective_reasoning_configuration", "semantic_launch_argv", "cwd_role", "cwd_relative_path",
        "provider_environment_fingerprint", "session_mode", "config_source", "config_sha256",
        "sampling_identity_verifiable", "invocation_fingerprint",
    )
    if any(
        baseline.get(key) is None or treatment.get(key) is None
        for key in actual_identity_fields
    ):
        return {
            "consistent": False,
            "intervention_only_repair_context": False,
            "provider_identity_observed": False,
            "reason": "actual trial CLI/model/reasoning identity is incomplete",
        }
    if baseline.get("sampling_identity_verifiable") is not True or treatment.get("sampling_identity_verifiable") is not True:
        return {
            "consistent": False,
            "intervention_only_repair_context": False,
            "provider_identity_observed": False,
            "reason": "effective sampling configuration could not be verified from actual invocation/config",
        }
    actual_identity_equal = all(
        baseline[key] == treatment[key] for key in actual_identity_fields
    )
    same_base = (
        baseline["input_without_repair_sha256"] == treatment["input_without_repair_sha256"]
        and baseline["decision_schema_sha256"] == treatment["decision_schema_sha256"]
        and baseline["checkpoint_id"] == treatment["checkpoint_id"]
        and baseline["provider_configuration_fingerprint"] == treatment["provider_configuration_fingerprint"]
        and actual_identity_equal
    )
    independent = (
        baseline["trial_id"] != treatment["trial_id"]
        and baseline["run_id"] != treatment["run_id"]
        and baseline["workspace_id"] != treatment["workspace_id"]
    )
    different_intervention = baseline["context_projection_sha256"] != treatment["context_projection_sha256"]
    passed = bool(same_base and independent and different_intervention)
    return {
        "consistent": passed,
        "intervention_only_repair_context": passed,
        "baseline_without_repair_sha256": baseline["input_without_repair_sha256"],
        "treatment_without_repair_sha256": treatment["input_without_repair_sha256"],
        "baseline_model_input_sha256": baseline["model_input_sha256"],
        "treatment_model_input_sha256": treatment["model_input_sha256"],
        "decision_schema_sha256_equal": baseline["decision_schema_sha256"] == treatment["decision_schema_sha256"],
        "actual_provider_identity_equal": actual_identity_equal,
        "provider_identity_observed": True,
        "preflight_identity_matches_trial": {
            "baseline": baseline.get("preflight_identity_matches_trial"),
            "treatment": treatment.get("preflight_identity_matches_trial"),
        },
        "independent_trials_and_workspaces": independent,
        "reason": "" if passed else "base input, configuration, fork identity, or RepairContext boundary differs",
    }


def _flatten(value: Any, path: str = "repair_context") -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        result = []
        for key in sorted(value):
            result.extend(_flatten(value[key], f"{path}.{key}"))
        return result
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            result.extend(_flatten(item, f"{path}[{index}]"))
        return result
    return [(path, value)]


def build_repair_context_provenance(
    repair_context: Sequence[Any], validation_attempt: Mapping[str, Any], contract: Mapping[str, Any],
    *, producer_artifact_id: str,
) -> list[dict[str, Any]]:
    if not producer_artifact_id or not validation_attempt.get("producer_run_id"):
        raise CheckpointError("RepairContext provenance lacks producer artifact/run identity")
    records = []
    for failure_index, failure in enumerate(repair_context):
        value = failure.to_dict() if hasattr(failure, "to_dict") else dict(failure)
        for field_path, field_value in _flatten(value, f"repair_context[{failure_index}]"):
            source_kind, source_ref, source_value, transformation = _provenance_source(
                field_path, field_value, value, validation_attempt, contract,
            )
            if digest_json(source_value) != digest_json(field_value):
                raise CheckpointError(
                    f"RepairContext value does not match resolved source: {field_path}"
                )
            record = {
                "producer_artifact_id": producer_artifact_id,
                "producer_run_id": validation_attempt["producer_run_id"],
                "validation_attempt_id": validation_attempt["attempt_id"],
                "field_path": field_path,
                "source_evidence_reference": source_ref,
                "source_kind": source_kind,
                "source_excerpt_or_value": source_value,
                "transformation": transformation,
                "rendered_value_sha256": _sha(yaml.safe_dump(field_value, allow_unicode=True, sort_keys=False)),
            }
            records.append(record)
    return records


def _provenance_source(field_path, field_value, failure, attempt, contract):
    attempt_id = str(attempt.get("attempt_id", ""))
    findings = attempt.get("findings", ())
    assertions = attempt.get("assertion_results", ())
    verification = attempt.get("verification_evidence", ())
    changed = attempt.get("changed_files", ())
    workspace = attempt.get("workspace_identity", {})
    relative_path = field_path.split("].", 1)[-1]
    if relative_path == "failure_id":
        boundary_code = "ASSERTION_FAILED" if failure.get("failure_boundary") == "ASSERTION" else "verification_failed"
        finding = next((item for item in findings if item.get("code") == boundary_code), None)
        if finding is None:
            raise CheckpointError("RepairContext failure has no matching Validation finding")
        value = repair_failure_identity(
            attempt_id, workspace.get("fingerprint_after", ""), failure.get("change_id"),
            failure.get("failure_boundary"), failure.get("assertion_id"), failure.get("claim_id"),
        )
        return "validation_result", f"validation_result:{attempt_id}:{boundary_code}", value, "failure_identity"
    if relative_path == "failure_boundary":
        code = "ASSERTION_FAILED" if failure.get("failure_boundary") == "ASSERTION" else "verification_failed"
        finding = next((item for item in findings if item.get("code") == code), None)
        if finding is None:
            raise CheckpointError("RepairContext failure boundary has no Validation finding")
        value = "ASSERTION" if code == "ASSERTION_FAILED" else "VERIFICATION"
        return "validation_result", f"validation_result:{attempt_id}:{code}", value, "failure_boundary_from_finding"
    if relative_path == "change_id":
        if not any(item.get("id") == field_value for item in contract.get("changes", ())):
            raise CheckpointError("RepairContext change id is absent from Contract")
        return "contract", f"contract:changes:{field_value}:id", field_value, "identity"
    if relative_path == "assertion_id":
        result = next((item for item in assertions if item.get("assertion_id") == field_value), None)
        if result is None:
            raise CheckpointError("RepairContext assertion has no AssertionResult source")
        return "assertion_result", f"assertion_result:{field_value}:assertion_id", result["assertion_id"], "identity"
    if relative_path == "claim_id":
        result = next((item for item in verification if item.get("claim_id") == field_value), None)
        if result is None:
            raise CheckpointError("RepairContext claim has no VerificationEvidence source")
        return "verification_evidence", f"verification_evidence:{field_value}:claim_id", result["claim_id"], "identity"
    if relative_path.startswith("expected."):
        key = relative_path[len("expected."):]
        config = _assertion_config(contract, failure.get("assertion_id"))
        source = config.get("expected") if isinstance(config, Mapping) else None
        if source is None:
            item = next((item for item in verification if item.get("claim_id") == failure.get("claim_id")), {})
            source = item.get("expected_result")
            source_kind = "verification_evidence"
            source_ref = f"verification_evidence:{failure.get('claim_id')}:expected_result"
        else:
            source_kind = "contract"
            source_ref = f"contract:assertions:{failure.get('assertion_id')}:config.expected"
        value = source
        for part in key.split("."):
            if not isinstance(value, Mapping) or part not in value:
                raise CheckpointError("RepairContext expected field is absent from authoritative source")
            value = value[part]
        return source_kind, source_ref, value, "json_field_extraction"
    if relative_path.startswith("observed."):
        key = relative_path[len("observed."):]
        result = next((item for item in assertions if item.get("assertion_id") == failure.get("assertion_id")), {})
        if key == "assertion_status":
            value = result.get("status")
            ref = f"assertion_result:{failure.get('assertion_id')}:status"
            if value is None:
                raise CheckpointError("RepairContext observed status has no AssertionResult")
            return "assertion_result", ref, value, "identity"
        verification_item = next((item for item in verification if item.get("claim_id") == failure.get("claim_id")), {})
        metadata = verification_item.get("execution_metadata", {})
        execution_id = verification_item.get("execution_result_id", "")
        if key == "diagnostic_excerpt":
            excerpt = metadata.get("stdout_preview_tail") or metadata.get("stderr_preview_tail")
            if not isinstance(excerpt, str) or not excerpt:
                raise CheckpointError("RepairContext diagnostic excerpt has no ExecutionResult source")
            return "execution_result", f"execution_result:{execution_id}:bounded_stream_tail_512", excerpt, "bounded_excerpt_512"
        if key == "detail":
            value = str(result.get("message", ""))[:512]
            return "assertion_result", f"assertion_result:{failure.get('assertion_id')}:message", value, "bounded_excerpt_512"
        raw = metadata.get("stdout_preview", "")
        try:
            payload = json.loads(raw)
            observed = payload.get("observed", {})
            value = observed
            for part in key.split("."):
                value = value[part]
        except (TypeError, ValueError, KeyError):
            actual = verification_item.get("actual_result", {})
            value = actual
            for part in key.split("."):
                if not isinstance(value, Mapping) or part not in value:
                    raise CheckpointError("RepairContext observed field has no source in probe or VerificationEvidence")
                value = value[part]
        return "execution_result", f"execution_result:{execution_id}:stdout_preview:json.observed.{key}", value, "json_field_extraction"
    if relative_path.startswith("evidence_refs["):
        if field_value not in set(ref for item in assertions for ref in item.get("evidence_refs", ())) | {
            f"{prefix}:{item[key]}" for item in verification for key, prefix in (
                ("claim_id", "claim"), ("request_id", "request"), ("action_id", "action"), ("execution_result_id", "execution_result"),
            ) if item.get(key)
        } | {f"assertion:{item['assertion_id']}" for item in assertions if item.get("assertion_id")}:
            raise CheckpointError("RepairContext evidence reference is absent from ValidationEvidence")
        return "validation_evidence", f"validation_evidence:{attempt_id}:references", field_value, "identity"
    if relative_path.startswith("changed_files["):
        normalized = str(field_value).replace("\\", "/")
        if normalized not in {str(item).replace("\\", "/") for item in changed}:
            raise CheckpointError("RepairContext changed file is absent from ValidationEvidence")
        return "changed_files", f"changed_files:{attempt_id}", normalized, "canonical_path"
    if relative_path.startswith("workspace_identity."):
        key = field_path.rsplit(".", 1)[1]
        if key not in workspace or workspace[key] != field_value:
            raise CheckpointError("RepairContext workspace identity differs from ValidationEvidence")
        return "validation_evidence", f"validation_evidence:{attempt_id}:workspace_identity:{key}", workspace[key], "identity"
    if relative_path == "changed_files_omitted":
        value = max(0, len(changed) - 20)
        return "validation_evidence", f"validation_evidence:{attempt_id}:changed_files", value, "omitted_path_count"
    raise CheckpointError(f"unsupported RepairContext provenance field: {field_path}")


def _assertion_config(contract: Mapping[str, Any], assertion_id: str | None) -> Any:
    verification = contract.get("verification", {})
    item = next((item for item in verification.get("assertions", ()) if item.get("id") == assertion_id), {})
    return item.get("config")


def validate_repair_provenance(
    repair_context: Sequence[Any], provenance: Sequence[Mapping[str, Any]], *,
    validation_attempt: Mapping[str, Any], contract: Mapping[str, Any],
    producer_artifact_id: str,
) -> bool:
    if not repair_context:
        return not provenance
    try:
        expected_records = build_repair_context_provenance(
            repair_context, validation_attempt, contract,
            producer_artifact_id=producer_artifact_id,
        )
    except (CheckpointError, KeyError, TypeError, ValueError):
        return False
    if digest_json(list(provenance)) != digest_json(expected_records):
        return False
    for record in expected_records:
        if record["transformation"] not in {
            "failure_identity", "failure_boundary_from_finding", "identity",
            "json_field_extraction", "bounded_excerpt_512", "canonical_path",
            "omitted_path_count",
        }:
            return False
    return True


def validate_repair_projection(
    prompt: str, repair_context: Sequence[Any], provenance: Sequence[Mapping[str, Any]], *,
    validation_attempt: Mapping[str, Any], contract: Mapping[str, Any],
    producer_artifact_id: str,
) -> bool:
    region = _repair_region(prompt)
    if region is None:
        return False
    try:
        actual = yaml.safe_load(region[1])
    except yaml.YAMLError:
        return False
    expected = [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in repair_context]
    if digest_json(actual) != digest_json(expected) or not validate_repair_provenance(
        repair_context, provenance,
        validation_attempt=validation_attempt,
        contract=contract,
        producer_artifact_id=producer_artifact_id,
    ):
        return False
    return True


def _validation_attempt_payload(attempt, contract: ChangeContract, run_id: str) -> dict[str, Any]:
    evidence = attempt.evidence
    verification = []
    for item in evidence.verification:
        bounded_metadata = {}
        for key in ("stdout_preview", "stderr_preview", "returncode"):
            if key not in item.execution_result.metadata:
                continue
            value = item.execution_result.metadata[key]
            bounded_metadata[key] = str(value)[:512] if isinstance(value, str) else value
            if isinstance(value, str) and key in {"stdout_preview", "stderr_preview"}:
                bounded_metadata[f"{key}_tail"] = value[-512:]
        verification.append({
            "claim_id": item.claim_id,
            "request_id": item.request_id,
            "action_id": item.action_id,
            "execution_result_id": item.execution_result_id,
            "argv": list(item.argv),
            "expected_result": dict(item.expected_result),
            "actual_result": dict(item.actual_result),
            "passed": item.passed,
            "execution_status": item.execution_result.status,
            "execution_success": item.execution_result.success,
            "execution_metadata": bounded_metadata,
        })
    assertions = [
        {
            "assertion_id": item.assertion_id,
            "change_id": item.change_id,
            "assertion_type": item.assertion_type.value,
            "status": item.status.value,
            "evidence_refs": list(item.evidence_refs),
            "message": item.message[:512],
        }
        for item in attempt.result.assertion_results
    ]
    findings = [
        {"rule": item.rule, "code": item.code, "message": item.message[:512], "path": item.path, "severity": item.severity}
        for item in attempt.result.findings
    ]
    base = {
        "attempt_id": attempt.attempt_id,
        "status": attempt.result.status,
        "repair_failure_ids": list(attempt.repair_failure_ids),
        "action_ids": list(attempt.action_ids),
        "failure_contexts": [item.to_dict() for item in attempt.failure_contexts],
        "findings": findings,
        "assertion_results": assertions,
        "verification_evidence": verification,
        "execution_results": [
            {
                "action_id": item.action_id,
                "status": item.status,
                "result_fingerprint": item.result_fingerprint,
                "metadata_sha256": digest_json(item.metadata),
            }
            for item in evidence.execution_results
        ],
        "workspace_identity": asdict(evidence.workspace),
        "changed_files": list(evidence.change_context.changed_files),
        "change_context": [asdict(item) for item in evidence.change_context.files],
        "producer_run_id": run_id,
    }
    base["repair_issue_descriptions"] = [
        HarnessOrchestrator._repair_issue_description(finding, evidence)[:512]
        for finding in attempt.result.findings
        if finding.severity == "error"
    ]
    return base


def _snapshot_observation(attempt_payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "verification": attempt_payload["verification_evidence"],
        "assertions": attempt_payload["assertion_results"],
        "validation": attempt_payload["findings"],
        "workspace_identity": attempt_payload["workspace_identity"],
    }


class _RunCapture:
    def __init__(self, record: TrialRecord | None = None):
        self.context = None
        self.states: list[AgentState] = []
        self.record = record

    def on_context(self, context):
        self.context = context

    def on_runtime_state(self, state):
        snapshot = AgentState.from_dict(state.to_dict())
        self.states.append(snapshot)
        if self.record is not None and self.record.payload.get("run_id") != snapshot.run_id:
            self.record.update(run_id=snapshot.run_id)


class _TrialPersistingTraceManager(TraceManager):
    """Persist only confirmed execution boundaries, not a second full trace."""

    _PERSISTED_EVENTS = {
        "action_decided", "tool_policy_evaluated", "tool_policy_decided",
        "approval_requested", "approval_resolved", "tool_execution_started",
        "tool_execution_completed", "action_executed", "observation_received",
        "validation_failed", "task_finished", "state_updated",
    }

    def __init__(self, run_id, writer, record):
        super().__init__(run_id, writer)
        self._trial_record = record

    def emit(self, event_type: str, data: dict) -> None:
        super().emit(event_type, data)
        if event_type not in self._PERSISTED_EVENTS:
            return
        event = self.get_events()[-1]
        try:
            self._trial_record.runtime_trace_boundary({
                "event": event_type,
                "sequence": event.sequence,
                "timestamp": event.timestamp,
                "data": event.data,
            })
        except Exception:
            # Trial diagnostics are best effort and never alter Action behavior.
            pass


class _PrefixAgent:
    """Deterministic canonical prefix which edits without fixing the target behavior."""

    def __init__(self, path: str):
        self.path = path

    def decide(self, state, _context):
        if not state.recent_actions:
            return Action.create_operation(OperationKind.FILE_READ, path=self.path, reason="inspect the target")
        last = state.recent_actions[-1]
        if last.operation.kind is OperationKind.FILE_READ:
            observation = next(item for item in reversed(state.recent_observations) if item.action_id == last.id)
            content = str(observation.metadata["content_preview"])
            return Action.create_operation(
                OperationKind.FILE_REPLACE,
                path=self.path,
                expected_text=content,
                replacement=content + "\n# deterministic failed-validation prefix\n",
                observed_file_hash=observation.metadata["file_hash"],
                reason="record a real but behavior-preserving change",
            )
        return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="end deterministic prefix")


class _SnapshotWorkspaceManager(WorkspaceManager):
    def __init__(self, approval, snapshot_path: Path):
        super().__init__()
        self._approval = approval
        self.snapshot_path = Path(snapshot_path)
        self.patch_text = ""

    def create(self, repo_path: Path):
        workspace = super().create(repo_path)
        self._approval.bind_workspace(workspace.path)
        return workspace

    def save_patch(self, workspace):
        path = super().save_patch(workspace)
        self.patch_text = path.read_text(encoding="utf-8")
        return path

    def cleanup(self, workspace):
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        if self.snapshot_path.exists():
            raise CheckpointError(f"checkpoint snapshot already exists: {self.snapshot_path}")
        shutil.copytree(workspace.path, self.snapshot_path)
        super().cleanup(workspace)


def _prepare_source(task_id: str, target: Path) -> tuple[Any, str]:
    task = next(item for item in real_model_repair_tasks() if item.id == task_id)
    source = Path(target)
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_ROOT / task.repository_snapshot, source)
    revision = BenchmarkRunner._initialize_repository(source)
    return task, revision


def prepare_readiness_checkpoint(
    task_id: str,
    output_dir: Path,
    *,
    experiment_id: str | None = None,
    pair_id: str | None = None,
) -> FailureCheckpoint:
    """Create one checkpoint only after a real canonical execution and real FAIL."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    task = next((item for item in real_model_repair_tasks() if item.id == task_id), None)
    if task is None or task_id not in {"behavior_repair", "verification_repair"}:
        raise ValueError("Phase 4D readiness accepts only the two declared readiness tasks")
    experiment_id = experiment_id or "exp:" + uuid.uuid4().hex
    pair_id = pair_id or "pair:" + uuid.uuid4().hex
    prefix_id = "trial:prefix:" + uuid.uuid4().hex
    run_id = "pending-prefix-run"
    # Keep temporary repository paths short: Memory artifacts and Git object
    # paths can otherwise hit legacy Windows MAX_PATH when pytest nests tmp_path.
    source = output_dir / "p" / "repo"
    task, baseline_revision = _prepare_source(task_id, source)
    baseline_fingerprint = _tracked_content_fingerprint(source)
    task_file = output_dir / "inputs" / f"{task_id}.md"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(task.description, encoding="utf-8")
    snapshot_nonce = uuid.uuid4().hex[:8]
    snapshot_path = output_dir / "c" / snapshot_nonce / "w"
    approval = _ScopedFixtureApproval(task)
    manager = _SnapshotWorkspaceManager(approval, snapshot_path)
    record = TrialRecord.create(
        output_dir / "trials" / f"{prefix_id.replace(':', '-')}.json",
        experiment_id=experiment_id, pair_id=pair_id, checkpoint_id="pending",
        trial_id=prefix_id, run_id=run_id, condition="prefix", task_id=task_id,
    )
    capture = _RunCapture(record)
    record.transition("STARTED", boundary="deterministic-prefix")
    result = HarnessOrchestrator(
        contract_agent=ContractAgent(generator=lambda _task, _repo: task.to_contract()),
        coding_agent=_PrefixAgent(task.expected_files[0]),
        workspace_manager=manager,
        approval_provider=approval,
        max_repairs=0,  # pause after the first real failed Validation; pair budget is frozen below
        runtime_max_steps=10,
        observer=capture,
        trace_manager_factory=lambda trace_run_id, writer: _TrialPersistingTraceManager(
            trace_run_id, writer, record,
        ),
    ).run(task_file, source)
    run_id = capture.states[-1].run_id if capture.states else ""
    trace_source = source / ".agent" / "runs" / run_id / "trace.json"
    trace_snapshot = snapshot_path / ".agent" / "runs" / run_id / "trace.json"
    if trace_source.is_file():
        trace_snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(trace_source, trace_snapshot)
    record.transition("HARNESS_TERMINATED", harness_success=result.success, validation_result=result.validation_result)
    record.transition("FINAL_STATE_EXPORTED", snapshot_path=str(snapshot_path))

    if (
        result.validation_result != "FAIL"
        or result.validator_result is None
        or not result.validation_attempts
        or not result.validation_attempts[-1].failure_contexts
        or capture.context is None
        or not capture.states
        or capture.states[-1].status != "FAILED"
        or not manager.patch_text
        or not snapshot_path.is_dir()
    ):
        record.update(failure_boundary="prefix_did_not_reach_repairable_validation_failure")
        raise CheckpointError("deterministic prefix did not produce a real repairable Validation failure")

    contract_payload = result.contract.model_dump(mode="json")
    attempt = result.validation_attempts[-1]
    attempt_payload = _validation_attempt_payload(attempt, result.contract, run_id)
    repair_context = [item.to_dict() for item in attempt.failure_contexts]
    provenance = build_repair_context_provenance(
        attempt.failure_contexts, attempt_payload, contract_payload,
        producer_artifact_id=prefix_id,
    )
    if not validate_repair_provenance(
        attempt.failure_contexts, provenance, validation_attempt=attempt_payload,
        contract=contract_payload, producer_artifact_id=prefix_id,
    ):
        raise CheckpointError("RepairContext provenance could not be established from producer evidence")
    if not trace_source.is_file():
        raise CheckpointError("canonical producer execution Trace is missing")
    record.update(producer_truth={
        "agent_state": capture.states[-1].to_dict(),
        "validation_attempt": attempt_payload,
        "contract": contract_payload,
        "plan": result.plan.to_dict(),
        "context": capture.context.to_dict(),
        "decision_seq": capture.states[-1].step_count,
        "configured_prefix_repairs": 0,
        "repairs_consumed": result.repairs,
        "steps_total": capture.states[-1].max_steps,
        "repairs_total": 2,
        "workspace_snapshot_path": str(snapshot_path.resolve()),
        "workspace_content_fingerprint": workspace_fingerprint(snapshot_path),
        "prefix_patch_fingerprint": _sha(manager.patch_text),
        "trace_sha256": _sha(trace_source.read_bytes()),
    })
    manifest = workspace_manifest(snapshot_path)
    payload = {
        "artifact_root": str(output_dir),
        "experiment_id": experiment_id,
        "pair_id": pair_id,
        "task": {"id": task.id, "description": task.description},
        "fixture": {
            "id": task.repository_snapshot,
            "baseline_revision": baseline_revision,
            "baseline_content_fingerprint": baseline_fingerprint,
        },
        "workspace": {
            "snapshot_path": str(snapshot_path),
            "manifest": manifest,
            "content_fingerprint": digest_json(manifest),
            "changed_files": list(attempt.evidence.change_context.changed_files),
            "prefix_patch_fingerprint": _sha(manager.patch_text),
        },
        "producer": {
            "trial_id": prefix_id,
            "run_id": run_id,
            "decision_seq": capture.states[-1].step_count,
            "harness_validation_attempt_id": attempt.attempt_id,
            "configured_prefix_repairs": 0,
        },
        "contract": contract_payload,
        "plan": result.plan.to_dict(),
        "context": capture.context.to_dict(),
        "agent_state": capture.states[-1].to_dict(),
        "validation_attempt": attempt_payload,
        "repair_context": repair_context,
        "repair_context_provenance": provenance,
        "budgets": {
            "steps_consumed": capture.states[-1].step_count,
            "steps_total": 10,
            "repairs_consumed": 0,
            "repairs_total": 2,
            "decision_timeout_seconds": 180,
            "provider_configuration_fingerprint": digest_json({
                "adapter": "CodexAgent", "execution_mode": "NATIVE", "timeout_seconds": 180,
                "schema": "canonical-action-v2",
            }),
        },
    }
    checkpoint = FailureCheckpoint.create({**payload, "schema_version": 1})
    checkpoint_path = output_dir / "c" / f"{snapshot_nonce}.json"
    checkpoint.save(checkpoint_path)
    record.update(checkpoint_id=checkpoint.checkpoint_id, checkpoint_path=str(checkpoint_path))
    return checkpoint


class _ReadinessRepairAgent(CodexAgent):
    """A deterministic decision source that uses the real Codex prompt renderer."""

def _trial_source(task_id: str, output_dir: Path, trial_id: str) -> tuple[Any, Path, str]:
    task = next(item for item in real_model_repair_tasks() if item.id == task_id)
    source = output_dir / f"s{_sha(trial_id)[:8]}" / "repo"
    _, revision = _prepare_source(task_id, source)
    return task, source, revision


def _run_trial_oracle(task_id: str, exported_workspace: Path, oracle_root: Path, expected_fingerprint: str) -> dict[str, Any]:
    """Run only after Harness return, against a copy of the exported final state."""
    source_fingerprint = workspace_fingerprint(exported_workspace)
    if source_fingerprint != expected_fingerprint:
        return {"execution_status": "ERROR", "pass": None, "error": "final export fingerprint mismatch"}
    oracle_root = Path(oracle_root)
    if oracle_root.exists():
        raise CheckpointError(f"oracle workspace already exists: {oracle_root}")
    oracle_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(exported_workspace, oracle_root)
    derived_fingerprint = workspace_fingerprint(oracle_root)
    if derived_fingerprint != source_fingerprint:
        return {"execution_status": "ERROR", "pass": None, "error": "derived oracle workspace mismatch"}

    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if task_id == "behavior_repair":
        code = (
            "import json; from src.auth import check_password; "
            "print(json.dumps({'accepted': bool(check_password(''))}))"
        )
        argv = [sys.executable, "-B", "-c", code]
    elif task_id == "verification_repair":
        argv = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_calculator.py"]
    else:
        return {"execution_status": "ERROR", "pass": None, "error": "unknown readiness oracle"}
    try:
        completed = subprocess.run(
            argv, cwd=oracle_root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "execution_status": "ERROR", "pass": None,
            "error": type(error).__name__, "error_detail_sha256": _sha(str(error)[:512]),
            "workspace_id": _sha(str(oracle_root))[:24],
        }
    after = workspace_fingerprint(exported_workspace)
    if after != source_fingerprint:
        return {"execution_status": "ERROR", "pass": None, "error": "oracle mutated exported final state"}
    if task_id == "behavior_repair":
        if completed.returncode != 0:
            return {
                "execution_status": "ERROR", "pass": None,
                "error": "behavior oracle process failed", "stderr_sha256": _sha(completed.stderr),
            }
        try:
            observed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {"execution_status": "ERROR", "pass": None, "error": "behavior oracle output malformed"}
        passed = observed == {"accepted": False}
    else:
        if completed.returncode not in {0, 1}:
            return {
                "execution_status": "ERROR", "pass": None,
                "error": "pytest oracle could not produce a test outcome",
                "returncode": completed.returncode,
            }
        passed = completed.returncode == 0
    return {
        "execution_status": "COMPLETED",
        "pass": bool(passed),
        "returncode": completed.returncode,
        "stdout_sha256": _sha(completed.stdout),
        "stderr_sha256": _sha(completed.stderr),
        "workspace_id": _sha(str(oracle_root))[:24],
        "source_final_fingerprint": source_fingerprint,
        "derived_workspace_fingerprint": derived_fingerprint,
    }


def _summarize_native_events(stdout: str, process_completed: bool) -> tuple[int | None, bool, list[str]]:
    native_types = {
        "command_execution", "file_change", "mcp_tool_call", "web_search_call",
        "local_shell_call", "function_call", "computer_call",
    }
    events = set()
    native_ids = set()
    parsed = 0
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        parsed += 1
        event_type = payload.get("type")
        if isinstance(event_type, str):
            events.add(event_type)
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") in native_types:
            native_ids.add(str(item.get("id") or (event_type, item["type"])))
    complete = process_completed and parsed > 0 and bool(events.intersection({"turn.completed", "response.completed", "thread.completed"}))
    count = len(native_ids) if native_ids or complete else None
    return count, complete, sorted(events)


class _InstrumentedCodexAgent(CodexAgent):
    def __init__(self, record: TrialRecord, checkpoint: FailureCheckpoint):
        timeout = int(checkpoint.payload["budgets"]["decision_timeout_seconds"])
        super().__init__(timeout=timeout)
        self.record = record
        self.checkpoint = checkpoint
        self.decision_seq = 0
        self.workspace_id = ""
        self._pending_provider_call_local_id = ""
        self._pending_started_at: float | None = None
        self._partial_stdout: str | bytes | None = None
        self._partial_stderr: str | bytes | None = None
        self._partial_response_artifact: str | bytes | None = None
        self._partial_capture_scope: str | None = None
        self._active_context = None
        self._active_state = None
        self._runtime_validation_source = None
        self._runtime_repair_provenance = ()
        self._pending_diagnostic_stdout = None
        self._pending_diagnostic_stderr = None
        self._pending_diagnostic_returncode = None
        self.trial_id = ""
        self.run_id = ""
        self._diagnostic_callback = self._record_provider_boundary

    def bind_execution_identity(self, *, workspace_id: str, run_id: str, trial_id: str) -> None:
        if run_id != self.record.payload["run_id"] or trial_id != self.record.payload["trial_id"]:
            raise ValueError("provider identity binding does not match the persisted trial")
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.trial_id = trial_id

    def _build_decision_prompt(self, state, context) -> str:
        return _MODEL_INPUT_PROVENANCE_GUIDANCE + super()._build_decision_prompt(state, context)

    def decide(self, state, context):
        self.decision_seq += 1
        self._active_context = context
        self._active_state = state
        try:
            action = super().decide(state, context)
        except Exception as error:
            if self._pending_provider_call_local_id:
                category = str(getattr(error, "category", type(error).__name__))
                call = self.record._provider_call(self._pending_provider_call_local_id)
                process_started = call.get("boundary_timestamps", {}).get("process_started") is not None
                if not process_started and isinstance(error, CheckpointError):
                    try:
                        self.record.provider_call_updated(
                            self._pending_provider_call_local_id,
                            setup_failure=category,
                            setup_failure_detail_sha256=_sha(str(error)[:512]),
                        )
                    except Exception as diagnostic_error:
                        self._diagnostic_recording_failed(
                            self._pending_provider_call_local_id, diagnostic_error,
                        )
                else:
                    try:
                        self.record.provider_call_failed(
                            self._pending_provider_call_local_id,
                            category=category,
                            detail_sha256=_sha(str(error)[:512]),
                            duration_seconds=(
                                None if self._pending_started_at is None
                                else perf_counter() - self._pending_started_at
                            ),
                            provider_status=_explicit_external_provider_status(str(error)),
                            stdout=self._partial_stdout,
                            stderr=self._partial_stderr,
                            response_artifact=self._partial_response_artifact,
                            capture_scope=self._partial_capture_scope,
                        )
                    except Exception as diagnostic_error:
                        self._diagnostic_recording_failed(
                            self._pending_provider_call_local_id, diagnostic_error,
                        )
            self._active_context = None
            self._active_state = None
            raise
        if self._pending_provider_call_local_id:
            try:
                self.record.provider_action_received(self._pending_provider_call_local_id, action.to_dict())
            except Exception as error:
                self._diagnostic_recording_failed(self._pending_provider_call_local_id, error)
        self._active_context = None
        self._active_state = None
        return action

    def on_runtime_state(self, _state):
        try:
            self.record.runtime_state_observed(_state)
        except Exception as error:
            self._diagnostic_recording_failed(self._pending_provider_call_local_id, error)

    def on_context(self, context):
        repair_context = tuple(getattr(context, "repair_context", ()) or ())
        if self._runtime_validation_source is not None:
            expected = tuple(self._runtime_validation_source["payload"].get("failure_contexts", ()))
            actual = tuple(item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in repair_context)
            if actual == expected:
                self._runtime_repair_provenance = tuple(self._runtime_validation_source["provenance"])

    def on_validation_attempt(self, attempt):
        if not attempt.failure_contexts or not self.run_id:
            return
        contract = ChangeContract.model_validate(self.checkpoint.payload["contract"])
        payload = _validation_attempt_payload(attempt, contract, self.run_id)
        artifact_id = f"validation-attempt:{attempt.attempt_id}"
        provenance = build_repair_context_provenance(
            attempt.failure_contexts, payload, contract.model_dump(mode="json"),
            producer_artifact_id=artifact_id,
        )
        self.record.validation_attempt_observed(attempt.attempt_id, payload)
        self._runtime_validation_source = {
            "payload": payload,
            "contract": contract.model_dump(mode="json"),
            "artifact_id": artifact_id,
            "provenance": provenance,
        }

    def _record_provider_boundary(self, boundary: str, **facts: Any) -> None:
        if self._pending_provider_call_local_id:
            call_id = self._pending_provider_call_local_id
            try:
                self.record.provider_boundary(call_id, boundary, **facts)
            except Exception as error:
                self._diagnostic_recording_failed(call_id, error)
            if boundary == "action_parsed":
                try:
                    count, complete, events = _summarize_native_events(
                        self._pending_diagnostic_stdout or "",
                        self._pending_diagnostic_returncode is not None,
                    )
                    current = self.record._provider_call(call_id).get("diagnostic_status")
                    self.record.provider_call_updated(
                        call_id,
                        native_tool_call_count=count,
                        native_call_observation_complete=complete,
                        provider_event_types=events,
                        diagnostic_status=(
                            current if current in {"DIAGNOSTIC_PARSE_ERROR", "DIAGNOSTIC_PERSISTENCE_FAILURE"}
                            else "COMPLETE"
                        ),
                    )
                except Exception as error:
                    self._diagnostic_recording_failed(call_id, error, parse_failure=True)

    def _diagnostic_recording_failed(self, call_id, error, *, parse_failure=False):
        try:
            self.record.provider_call_updated(
                call_id,
                diagnostic_status="DIAGNOSTIC_PARSE_ERROR" if parse_failure else "DIAGNOSTIC_PERSISTENCE_FAILURE",
                diagnostic_error_type=type(error).__name__,
                diagnostic_error_sha256=_sha(str(error)[:512]),
            )
        except Exception:
            pass

    def _model_visible_input_manifest(self, prompt, context, state, repair_context, provenance):
        if context is None or state is None or prompt != self._build_decision_prompt(state, context):
            raise CheckpointError("model-visible input differs from canonical state/context rendering")
        context_payload = context.to_dict()
        expected_context = dict(self.checkpoint.payload["context"])
        actual_context = dict(context_payload)
        for key in ("current_goal", "working_memory", "active_issues", "decisions", "repair_context"):
            expected_context.pop(key, None)
            actual_context.pop(key, None)
        expected_repo = dict(expected_context["repository_context"])
        actual_repo = dict(actual_context["repository_context"])
        expected_repo.pop("root", None)
        actual_repo.pop("root", None)
        expected_context["repository_context"] = expected_repo
        actual_context["repository_context"] = actual_repo
        if actual_context != expected_context:
            raise CheckpointError("model-visible static Context lacks checkpoint/public-source provenance")
        if (
            context.task_context.task_text != self.checkpoint.payload["task"]["description"]
            or state.task_description != self.checkpoint.payload["task"]["description"]
            or digest_json(context.contract) != digest_json(self.checkpoint.payload["contract"])
            or list(context.working_memory) != [item.summary for item in state.working_memory_summary[:10]]
            or list(context.active_issues) != [item.description for item in state.open_issues[:10]]
            or list(context.decisions) != state.decisions[:10]
            or context.current_goal != state.current_goal
        ):
            raise CheckpointError("model-visible dynamic Context is not derived from current Runtime state")
        state_payload = state.to_dict()
        def entry(section, field, trust_class, value, references=()):
            return {
                "section": section,
                "field": field,
                "trust_class": trust_class,
                "source_references": list(references),
                "rendered_sha256": _sha(yaml.safe_dump(value, allow_unicode=True, sort_keys=False)),
            }

        actions_ref = [f"action:{item.id}" for item in state.recent_actions]
        observations_ref = [f"observation:{item.action_id}" for item in state.recent_observations]
        return [
            entry("Task", "task", "PUBLIC_INPUT", context.task_context.task_text,
                  [f"task:{self.checkpoint.payload['task']['id']}"]),
            entry("Change Contract", "contract", "PUBLIC_INPUT", context.contract,
                  [f"contract:{digest_json(self.checkpoint.payload['contract'])}"]),
            entry("Context", "static_context", "UNVERIFIED_RUNTIME_SUMMARY", actual_context,
                  [f"checkpoint:{self.checkpoint.checkpoint_id}:context"]),
            entry("Runtime state", "current_goal", "VERIFIED_RUNTIME_FACT", state.current_goal,
                  [f"runtime_state:{state.run_id}:current_goal"]),
            entry("Runtime state", "status_and_counts", "VERIFIED_RUNTIME_FACT", {
                "status": state.status, "step_count": state.step_count,
                "action_count": len(state.recent_actions),
                "observation_count": len(state.recent_observations),
            }, actions_ref + observations_ref),
            entry("Runtime state", "errors", "UNVERIFIED_RUNTIME_SUMMARY", state.errors,
                  [f"runtime_state:{state.run_id}:errors"]),
            entry("Working Memory", "working_memory", "UNVERIFIED_RUNTIME_SUMMARY",
                  [item.summary for item in state.working_memory_summary[:10]], observations_ref),
            entry("Active Issues", "active_issues", "UNVERIFIED_RUNTIME_SUMMARY",
                  [item.description for item in state.open_issues[:10]],
                  [f"runtime_state:{state.run_id}:issues"]
                  + [f"issue:{item.id}" for item in state.open_issues[:10]]),
            entry("Recent Decisions", "decisions", "MODEL_GENERATED_UNVERIFIED", state.decisions[:10],
                  [f"runtime_state:{state.run_id}:decisions"]
                  + [f"action_reason:{item.id}" for item in state.recent_actions[:10]]),
            entry("Validation Repair Evidence", "repair_context", "VERIFIED_RUNTIME_FACT",
                  [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in repair_context],
                  [item["source_evidence_reference"] for item in provenance]
                  or [f"condition:{self.record.payload['condition']}:initial_repair_context"]),
            entry("Context Constraints", "constraints", "UNVERIFIED_RUNTIME_SUMMARY",
                  list(context.constraints), [f"checkpoint:{self.checkpoint.checkpoint_id}:context"]),
            {
                "section": "FULL_PROMPT",
                "field": "rendered_prompt",
                "trust_class": "COMPOSITE",
                "source_references": [],
                "rendered_sha256": _sha(prompt),
            },
        ]

    def _run_cli(self, command, *, prompt, workspace):
        self._pending_provider_call_local_id = "provider-call:" + uuid.uuid4().hex
        self._pending_started_at = perf_counter()
        self._partial_stdout = None
        self._partial_stderr = None
        self._partial_response_artifact = None
        self._partial_capture_scope = None
        call_id = self._pending_provider_call_local_id
        cwd = str(Path(workspace).resolve()) if workspace is not None else str(Path.cwd())
        # Persist the decision boundary before provenance, schema, executable,
        # or provider diagnostics can fail.
        self.record.provider_decision_started(
            decision_seq=self.decision_seq,
            provider_call_local_id=call_id,
            workspace_id=self.workspace_id,
            model_input_sha256=_sha(prompt),
            prompt_sha256=_sha(prompt),
            provider_configuration_fingerprint=self.checkpoint.payload["budgets"][
                "provider_configuration_fingerprint"
            ],
            cwd=cwd,
        )

        active_context = self._active_context
        repair_context = tuple(getattr(active_context, "repair_context", ()) or ())
        serialized_context = tuple(
            item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in repair_context
        )
        checkpoint_context = tuple(self.checkpoint.payload["repair_context"])
        runtime_source = self._runtime_validation_source
        runtime_context = tuple(runtime_source["payload"].get("failure_contexts", ())) if runtime_source else ()
        if self.decision_seq == 1 and self.record.payload["condition"] == "treatment":
            repair_context = self.checkpoint.restore_repair_context()
            provenance = self.checkpoint.payload["repair_context_provenance"]
            source_attempt = self.checkpoint.payload["validation_attempt"]
            source_contract = self.checkpoint.payload["contract"]
            source_artifact = self.checkpoint.payload["producer"]["trial_id"]
        elif serialized_context and serialized_context == checkpoint_context:
            provenance = self.checkpoint.payload["repair_context_provenance"]
            source_attempt = self.checkpoint.payload["validation_attempt"]
            source_contract = self.checkpoint.payload["contract"]
            source_artifact = self.checkpoint.payload["producer"]["trial_id"]
        elif serialized_context and runtime_source is not None and serialized_context == runtime_context:
            provenance = runtime_source["provenance"]
            source_attempt = runtime_source["payload"]
            source_contract = runtime_source["contract"]
            source_artifact = runtime_source["artifact_id"]
        elif serialized_context:
            provenance, source_attempt, source_contract, source_artifact = (), {}, {}, ""
        else:
            provenance, source_attempt, source_contract, source_artifact = (), {}, {}, ""
        repair_region = _repair_region(prompt)
        provenance_valid = bool(
            repair_region is not None
            and validate_repair_projection(
                prompt, repair_context, provenance,
                validation_attempt=source_attempt,
                contract=source_contract,
                producer_artifact_id=source_artifact,
            )
        )
        self.record.provider_call_updated(
            call_id,
            context_projection_sha256=_sha(repair_region[1]) if repair_region else "",
            input_without_repair_sha256=(
                _sha(repair_region[0] + repair_region[2]) if repair_region else ""
            ),
            repair_provenance_valid=provenance_valid,
            first_decision_intervention=(
                self.decision_seq == 1 and self.record.payload["condition"] == "treatment"
            ),
        )
        if not provenance_valid:
            self.record.provider_call_updated(call_id, setup_failure="repair_provenance_rejected")
            raise CheckpointError("actual rendered provider input violates the RepairContext provenance boundary")
        manifest = self._model_visible_input_manifest(
            prompt, active_context, self._active_state, repair_context, provenance,
        )
        self.record.provider_call_updated(call_id, model_visible_provenance=manifest)

        schema_path = None
        for index, argument in enumerate(command[:-1]):
            if argument == "--output-schema":
                schema_path = Path(command[index + 1])
                break
        if schema_path is None or not schema_path.is_file():
            self.record.provider_call_updated(call_id, setup_failure="action_schema_unavailable")
            raise CheckpointError("actual provider invocation has no readable Action schema")
        schema_hash = _sha(schema_path.read_bytes())
        try:
            provider_identity = _actual_provider_identity(
                command, cwd=workspace,
                workspace_root=(
                    self._active_state.workspace_reference
                    if self._active_state is not None and self._active_state.workspace_reference
                    else workspace
                ),
            )
        except Exception as error:
            # Identity capture is diagnostic: preserve uncertainty without
            # changing the command that the canonical adapter would execute.
            provider_identity = {}
            self.record.provider_call_updated(
                call_id, invocation_identity_error=type(error).__name__,
            )
        preflight = self.record.payload.get("preflight_identity", {})
        if not isinstance(preflight, Mapping):
            preflight = {}
        identity_values = dict(provider_identity)
        invocation_fingerprint = digest_json({
                "actual_executable_path": provider_identity["actual_executable_path"],
                "launch_path": provider_identity["launch_path"],
                "actual_cli_version": provider_identity["actual_cli_version"],
                "effective_model": provider_identity["effective_model"],
                "effective_reasoning_configuration": provider_identity["effective_reasoning_configuration"],
                "semantic_launch_argv": provider_identity["semantic_launch_argv"],
                "cwd_role": provider_identity["cwd_role"],
                "cwd_relative_path": provider_identity["cwd_relative_path"],
                "provider_environment_fingerprint": provider_identity["provider_environment_fingerprint"],
                "session_mode": provider_identity["session_mode"],
                "config_source": provider_identity["config_source"],
                "config_sha256": provider_identity["config_sha256"],
                "sampling_identity_verifiable": provider_identity["sampling_identity_verifiable"],
                "schema_sha256": schema_hash,
                "timeout_seconds": self._timeout,
                "execution_mode": self._execution_mode,
                "provider_configuration_fingerprint": self.checkpoint.payload["budgets"][
                    "provider_configuration_fingerprint"
                ],
            }) if provider_identity else None
        launch = provider_identity.get("launch_path")
        preflight_comparison = {
            "version": None if preflight.get("cli_version") is None or provider_identity.get("actual_cli_version") is None
            else preflight["cli_version"] == provider_identity["actual_cli_version"],
            "launch_path": None if preflight.get("launch_path") is None or launch is None
            else preflight["launch_path"] == launch,
            "model": None if preflight.get("model") is None or provider_identity.get("effective_model") is None
            else preflight["model"] == provider_identity["effective_model"],
            "reasoning_configuration": None
            if preflight.get("reasoning_configuration") is None or provider_identity.get("effective_reasoning_configuration") is None
            else preflight["reasoning_configuration"] == provider_identity["effective_reasoning_configuration"],
            "config_source": None if preflight.get("config_source") is None or provider_identity.get("config_source") is None
            else preflight["config_source"] == provider_identity["config_source"],
            "config_sha256": None if preflight.get("config_sha256") is None or provider_identity.get("config_sha256") is None
            else preflight["config_sha256"] == provider_identity["config_sha256"],
        }
        comparisons = list(preflight_comparison.values())
        preflight_match = False if any(value is False for value in comparisons) else (
            None if not comparisons or any(value is None for value in comparisons) else True
        )
        self.record.provider_call_updated(
            call_id,
            **identity_values,
            decision_schema_sha256=schema_hash,
            schema_sha256=schema_hash,
            invocation_fingerprint=invocation_fingerprint,
            preflight_cli_version=preflight.get("cli_version"),
            preflight_launch_path=preflight.get("launch_path"),
            preflight_model=preflight.get("model"),
            preflight_reasoning_configuration=preflight.get("reasoning_configuration"),
            preflight_config_source=preflight.get("config_source"),
            preflight_config_sha256=preflight.get("config_sha256"),
            preflight_identity_comparison=preflight_comparison,
            preflight_identity_matches_trial=preflight_match,
            preflight_actual_version_match=preflight_comparison["version"],
        )
        try:
            completed = super()._run_cli(command, prompt=prompt, workspace=workspace)
        except subprocess.TimeoutExpired as error:
            self._partial_stdout = getattr(error, "output", None)
            self._partial_stderr = getattr(error, "stderr", None)
            self._partial_response_artifact = self._read_response_artifact(command)
            self._partial_capture_scope = "timeout_exception_partial"
            self._emit_diagnostic(
                "assistant_output_artifact_checked",
                artifact_present=bool(self._partial_response_artifact),
                artifact_size_bytes=len(self._partial_response_artifact or b""),
                artifact_sha256=_sha(self._partial_response_artifact or b""),
            )
            raise
        except BaseException as error:
            self._partial_stdout = getattr(error, "output", None)
            self._partial_stderr = getattr(error, "stderr", None)
            self._partial_response_artifact = self._read_response_artifact(command)
            self._partial_capture_scope = "interrupted_process_partial"
            try:
                self.record.provider_call_interrupted(
                    self._pending_provider_call_local_id,
                    category=type(error).__name__,
                    stdout=self._partial_stdout,
                    stderr=self._partial_stderr,
                    response_artifact=self._partial_response_artifact,
                )
            except Exception as diagnostic_error:
                self._diagnostic_recording_failed(self._pending_provider_call_local_id, diagnostic_error)
            raise
        output_path = None
        for index, argument in enumerate(command[:-1]):
            if argument == "--output-last-message":
                output_path = Path(command[index + 1])
                break
        try:
            response_bytes = output_path.read_bytes() if output_path and output_path.is_file() else None
        except OSError as error:
            response_bytes = None
            self._diagnostic_recording_failed(self._pending_provider_call_local_id, error)
        self._partial_response_artifact = response_bytes
        response_present = bool(response_bytes)
        self._pending_diagnostic_stdout = completed.stdout or ""
        self._pending_diagnostic_stderr = completed.stderr or ""
        self._pending_diagnostic_returncode = completed.returncode
        try:
            self.record.provider_process_returned(
                self._pending_provider_call_local_id,
                returncode=completed.returncode,
                response_artifact_present=response_present,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                response_artifact=response_bytes,
                native_tool_call_count=None,
                native_call_observation_complete=False,
                event_types=(),
                duration_seconds=perf_counter() - self._pending_started_at,
            )
        except Exception as error:
            self._diagnostic_recording_failed(self._pending_provider_call_local_id, error)
        return completed

    @staticmethod
    def _read_response_artifact(command: Sequence[str]) -> bytes | None:
        for index, argument in enumerate(command[:-1]):
            if argument == "--output-last-message":
                path = Path(command[index + 1])
                try:
                    return path.read_bytes() if path.is_file() else None
                except OSError:
                    return None
        return None


class _PrefixObserver:
    """No-op provider-side observer for deterministic Action prefix creation."""

    pass


class _RepairAgentForReadiness:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.prompts: list[str] = []

    def decide(self, state, context):
        prompt = _MODEL_INPUT_PROVENANCE_GUIDANCE + CodexAgent(timeout=180)._build_decision_prompt(state, context)
        self.prompts.append(prompt)
        step = len(self.prompts)
        target = "src/auth.py" if self.task_id == "behavior_repair" else "src/calculator.py"
        if step == 1:
            return Action.create_operation(OperationKind.FILE_READ, path=target, reason="inspect repair target")
        previous = state.recent_actions[-1]
        if previous.operation.kind is OperationKind.FILE_READ:
            observation = next(item for item in reversed(state.recent_observations) if item.action_id == previous.id)
            replacement = (
                "def check_password(password):\n    return password == \"s3cure-pass\"\n"
                if self.task_id == "behavior_repair"
                else "def calculate_total(items):\n    return sum(items)\n"
            )
            return Action.create_operation(
                OperationKind.FILE_REPLACE, path=target,
                expected_text=str(observation.metadata["content_preview"]),
                replacement=replacement,
                observed_file_hash=str(observation.metadata["file_hash"]),
                reason="make the required behavior change",
            )
        return Action.create_operation(OperationKind.WORKFLOW_FINISH, reason="ask Harness to validate")


def _copy_trial_source(task_id: str, output_dir: Path, trial_id: str) -> tuple[Any, Path, str]:
    return _trial_source(task_id, output_dir, trial_id)


def _lineage_is_complete(checkpoint: FailureCheckpoint, result) -> bool:
    prefix = checkpoint.payload["validation_attempt"]
    if prefix["status"] != "FAIL" or not prefix["failure_contexts"]:
        return False
    if not result.harness_result.validation_attempts:
        return False
    first = result.harness_result.validation_attempts[0]
    return (
        result.checkpoint_validation_attempt_id == prefix["attempt_id"]
        and set(first.repair_failure_ids).issubset({item["failure_id"] for item in prefix["failure_contexts"]})
        and all(item.execution_result_id for item in first.evidence.verification)
    )


def _canonical_action_path_status(record: TrialRecord, *, real_model: bool) -> bool | None:
    if not real_model:
        return True
    calls = record.payload.get("provider_calls", ())
    if not calls:
        return None
    if any(isinstance(call.get("native_tool_call_count"), int) and call["native_tool_call_count"] > 0 for call in calls):
        return False
    if all(
        call.get("native_call_observation_complete") is True
        and call.get("native_tool_call_count") == 0
        for call in calls
    ):
        return True
    return None


def _terminal_evidence_completeness(harness_result, record: TrialRecord, oracle: Mapping[str, Any]) -> tuple[bool, str]:
    calls = record.payload.get("provider_calls", ())
    runtime = record.payload.get("runtime_state_snapshot", {})
    boundaries = record.payload.get("runtime_facts", {}).get("boundaries", ())
    events = {item.get("event") for item in boundaries}
    failure = harness_result.failure
    attempts = tuple(harness_result.validation_attempts)
    category = "" if failure is None else str(failure.category)
    stage = "" if failure is None else str(failure.stage)
    if oracle.get("execution_status") == "ERROR":
        complete = any(
            event.get("status") == "FINAL_STATE_EXPORTED"
            and event.get("final_workspace_fingerprint")
            for event in record.payload.get("events", ())
        )
        return complete, "oracle_failure" if complete else "oracle_failure_without_final_export"
    if harness_result.success:
        complete = bool(
            harness_result.validation_result in {"PASS", "WARNING"}
            and attempts
            and all(item.execution_result_id for attempt in attempts for item in attempt.evidence.verification)
        )
        return complete, "successful_harness_termination" if complete else "successful_termination_missing_validation_evidence"
    if category == "DECISION_ERROR" or stage == "ADAPTER":
        complete = bool(calls and calls[-1].get("boundary_timestamps", {}).get("decision_started"))
        return complete, "model_decision_failure" if complete else "decision_failure_without_provider_boundary"
    if "GOVERNANCE" in stage or "APPROVAL" in category or "POLICY" in category:
        complete = {"action_decided", "tool_policy_decided", "observation_received"}.issubset(events)
        return complete, "governance_rejection" if complete else "governance_rejection_missing_action_or_observation"
    if stage in {"TOOL_EXECUTION", "TOOL", "EXECUTION"}:
        decided = {
            item.get("data", {}).get("action_id") for item in boundaries
            if item.get("event") == "action_decided"
        }
        failed = {
            item.get("data", {}).get("action_id") for item in boundaries
            if item.get("event") == "action_executed"
            and item.get("data", {}).get("success") is False
        }
        observed = {
            item.get("data", {}).get("action_id") for item in boundaries
            if item.get("event") == "observation_received"
            and item.get("data", {}).get("failure_stage") == stage
        }
        complete = bool((decided & failed) & observed)
        return complete, "tool_failure" if complete else "tool_failure_missing_execution_lineage"
    if category == "MAX_STEPS":
        complete = runtime.get("failure_category") == "MAX_STEPS" and "state_updated" in events
        return complete, "budget_exhaustion" if complete else "budget_exhaustion_missing_runtime_terminal"
    if attempts:
        latest = attempts[-1]
        if latest.result.status == "FAIL":
            verification_ok = all(item.execution_result_id for item in latest.evidence.verification)
            assertion_ids = {item.assertion_id for item in latest.result.assertion_results}
            contract = harness_result.contract
            definitions = () if contract is None else contract.verification.assertions
            assertion_ok = all(item.id in assertion_ids for item in definitions)
            claim_ids = {item.claim_id for item in latest.evidence.verification}
            expected_claim_ids = set() if contract is None else {
                item.id for item in contract.verification.claims
            }
            claims_ok = expected_claim_ids.issubset(claim_ids)
            return bool(verification_ok and assertion_ok and claims_ok), "verification_or_validation_failure"
    if any(call.get("interruption_observed_at") for call in calls):
        return True, "provider_interruption_boundary_persisted"
    return False, "unclassified_or_incomplete_terminal_boundary"


def _run_one_fork(
    checkpoint: FailureCheckpoint,
    *,
    task_id: str,
    condition: str,
    output_dir: Path,
    pair_id: str,
    validate_prompt: bool = True,
    trial_id: str | None = None,
    run_id: str | None = None,
    real_model: bool = False,
) -> dict[str, Any]:
    trial_id = trial_id or f"trial:{condition}:{uuid.uuid4().hex}"
    run_id = run_id or f"{condition}-{uuid.uuid4().hex}"
    trial_slug = _sha(trial_id)[:10]
    final_workspace = output_dir / "f" / trial_slug / "w"
    record = TrialRecord.create(
        output_dir / "trials" / f"{trial_id.replace(':', '-')}.json",
        experiment_id=checkpoint.payload["experiment_id"], pair_id=pair_id,
        checkpoint_id=checkpoint.checkpoint_id, trial_id=trial_id,
        run_id=run_id, condition=condition, task_id=task_id,
    )
    try:
        source_task, source_repo, revision = _copy_trial_source(task_id, output_dir, trial_id)
    except Exception as error:
        record.update(setup_error={
            "stage": "fixture_setup",
            "error_type": type(error).__name__,
            "detail": str(error)[:512],
        })
        raise
    source_manager = _ScopedFixtureApproval(source_task)
    record.transition("STARTED", checkpoint_fingerprint=checkpoint.fingerprint)
    agent = _InstrumentedCodexAgent(record, checkpoint) if real_model else _RepairAgentForReadiness(task_id)
    harness = HarnessOrchestrator(
        coding_agent=agent,
        workspace_manager=_ApprovalBoundWorkspaceManager(source_manager),
        approval_provider=source_manager,
        max_repairs=int(checkpoint.payload["budgets"]["repairs_total"]),
        runtime_max_steps=int(checkpoint.payload["budgets"]["steps_total"]),
        observer=agent if real_model else None,
        trace_manager_factory=lambda trace_run_id, writer: _TrialPersistingTraceManager(
            trace_run_id, writer, record,
        ),
    )
    try:
        result = harness.repair_start(
            checkpoint,
            repo_path=source_repo,
            trial_id=trial_id,
            run_id=run_id,
            include_repair_context=(condition == "treatment"),
            final_workspace_path=final_workspace,
        )
    except Exception as error:
        record.update(harness_error={
            "stage": "repair_start",
            "error_type": type(error).__name__,
            "detail": str(error)[:512],
        })
        raise
    record.transition("HARNESS_TERMINATED", harness_success=result.harness_result.success,
                      validation_result=result.harness_result.validation_result)
    record.transition("FINAL_STATE_EXPORTED", final_workspace_fingerprint=result.final_workspace_fingerprint,
                      workspace_id=result.workspace_id)

    if isinstance(agent, _RepairAgentForReadiness) and agent.prompts:
        record.update(initial_model_prompt=agent.prompts[0],
                      first_model_input_sha256=_sha(agent.prompts[0]),
                      repair_projection_sha256=_sha((_repair_region(agent.prompts[0]) or ("", "", ""))[1]))
    oracle_dir = output_dir / "o" / trial_slug / "w"
    # The only call site is after repair_start returned and final state was exported.
    try:
        oracle = _run_trial_oracle(task_id, result.final_workspace_path, oracle_dir, result.final_workspace_fingerprint)
    except Exception as error:
        oracle = {
            "execution_status": "ERROR",
            "pass": None,
            "error": "oracle_runner_exception",
            "error_type": type(error).__name__,
            "error_detail_sha256": _sha(str(error)[:512]),
        }
    record.transition("ORACLE_COMPLETED", execution_status=oracle.get("execution_status"), pass_value=oracle.get("pass"))
    success = primary_success(result.harness_result.success, oracle.get("pass"))
    attempt_lineage = [
        {
            "trial_id": trial_id,
            "run_id": run_id,
            "workspace_id": result.workspace_id,
            "stage": "repair_validation",
            "attempt_id": attempt.attempt_id,
            "status": attempt.result.status,
            "failure_ids_consumed": list(attempt.repair_failure_ids),
            "repair_failure_ids_produced": [item.failure_id for item in attempt.failure_contexts],
            "execution_result_ids": [item.execution_result_id for item in attempt.evidence.verification],
            "assertion_result_ids": [item.assertion_id for item in attempt.result.assertion_results],
        }
        for attempt in result.harness_result.validation_attempts
    ]
    observed_attempts = [
        item for item in record.payload.get("evidence_lineage", ())
        if item.get("kind") == "validation_attempt"
    ]
    record.payload["evidence_lineage"] = [{
        "source": "checkpoint",
        "stage": "prefix_validation_failure",
        "checkpoint_id": checkpoint.checkpoint_id,
        "producer_run_id": checkpoint.payload["producer"]["run_id"],
        "producer_trial_id": checkpoint.payload["producer"]["trial_id"],
        "validation_attempt_id": checkpoint.payload["validation_attempt"]["attempt_id"],
        "failure_ids": [item["failure_id"] for item in checkpoint.payload["repair_context"]],
        "evidence_refs": [item for failure in checkpoint.payload["repair_context"] for item in failure["evidence_refs"]],
        "consumed_by_trial_id": trial_id,
    }, *observed_attempts, *attempt_lineage]
    setup_checks = {
        "checkpoint_valid": True,
        "baseline_revision_match": revision == checkpoint.payload["fixture"]["baseline_revision"],
        "start_fingerprint_match": result.starting_workspace_fingerprint == checkpoint.payload["workspace"]["content_fingerprint"],
        "oracle_scores_exported_state": oracle.get("source_final_fingerprint") == result.final_workspace_fingerprint,
        "oracle_state_isolated": oracle_dir.resolve() != result.final_workspace_path.resolve(),
        "approval_bound_to_restored_workspace": source_manager.workspace is not None,
    }
    provider_calls = record.payload.get("provider_calls", ())
    sampling_identity = (
        provider_calls[0].get("sampling_identity_verifiable")
        if real_model and provider_calls else False if real_model else None
    )
    setup_checks["provider_sampling_identity_verifiable"] = sampling_identity
    harness_result = result.harness_result
    model_outcome = _model_outcome(harness_result, oracle)
    evidence_complete, terminal_boundary = _terminal_evidence_completeness(
        harness_result, record, oracle,
    )
    canonical_path_status = _canonical_action_path_status(record, real_model=real_model)
    native_bypass_observed = any(
        isinstance(call.get("native_tool_call_count"), int)
        and call["native_tool_call_count"] > 0
        for call in record.payload.get("provider_calls", ())
    )
    setup_checks["canonical_action_path"] = canonical_path_status
    facts = {
        "setup_checks": setup_checks,
        "oracle_execution_status": oracle.get("execution_status"),
        "oracle_wiring_error": oracle.get("execution_status") == "ERROR",
        "evidence_complete": evidence_complete,
        "terminal_boundary": terminal_boundary,
        "execution_completed": any(
            item.get("event") in {"tool_execution_completed", "observation_received", "task_finished"}
            for item in record.payload.get("runtime_facts", {}).get("boundaries", ())
        ),
        "timeout_observed": any(
            call.get("boundary_timestamps", {}).get("deadline_fired") is not None
            or "timeout" in str(call.get("error_category", "")).lower()
            for call in record.payload.get("provider_calls", ())
        ),
        "provider_status": next((
            call["provider_status"] for call in record.payload.get("provider_calls", ())
            if call.get("provider_status")
        ), ""),
        "model_outcome": model_outcome,
    }
    validity = classify_sample(facts)
    record.update(
        workspace_id=result.workspace_id,
        starting_workspace_fingerprint=result.starting_workspace_fingerprint,
        final_workspace_fingerprint=result.final_workspace_fingerprint,
        steps_consumed_at_start=int(checkpoint.payload["budgets"]["steps_consumed"]),
        steps_consumed_at_end=result.runtime_state.step_count,
        steps_total=int(checkpoint.payload["budgets"]["steps_total"]),
        repairs_consumed_at_start=int(checkpoint.payload["budgets"]["repairs_consumed"]),
        repairs_consumed_at_end=harness_result.repairs,
        repairs_total=int(checkpoint.payload["budgets"]["repairs_total"]),
        retry_count_at_end=result.runtime_state.retry_count,
        checkpoint_fingerprint=checkpoint.fingerprint,
        checkpoint_validation_attempt_id=result.checkpoint_validation_attempt_id,
        repair_context_included=(condition == "treatment"),
        repair_context_provenance_sha256=digest_json(checkpoint.payload["repair_context_provenance"] if condition == "treatment" else []),
        validation_attempts=attempt_lineage,
        evidence_complete=evidence_complete,
        terminal_boundary=terminal_boundary,
        oracle=oracle,
        harness_result={
            "success": harness_result.success,
            "validation_result": harness_result.validation_result,
            "final_outcome": harness_result.final_outcome,
            "termination_reason": harness_result.termination_reason,
            "failure": None if harness_result.failure is None else harness_result.failure.to_dict(),
        },
        **success,
        setup_checks=setup_checks,
        validity=validity.value,
        classifier_version=CLASSIFIER_VERSION,
        model_outcome=model_outcome,
    )
    record.transition("CLASSIFIED", validity=validity.value, primary_success=success["primary_success"])
    prefix_action_ids = set(checkpoint.payload["validation_attempt"].get("action_ids", ()))
    record.update(
        provider_applicable=bool(real_model),
        canonical_action_path_preserved=canonical_path_status,
        native_effectful_bypass_observed=native_bypass_observed,
        action_sequence=[action.operation.kind.value for action in result.runtime_state.recent_actions],
        trial_action_sequence=[
            action.operation.kind.value for action in result.runtime_state.recent_actions
            if action.id not in prefix_action_ids
        ],
    )
    return record.payload


def _model_outcome(harness_result, oracle: Mapping[str, Any]) -> str:
    if oracle.get("execution_status") == "ERROR":
        return "oracle_failure"
    if harness_result.success and oracle.get("pass") is True:
        return "completed"
    failure = harness_result.failure
    category = "" if failure is None else str(failure.category)
    stage = "" if failure is None else str(failure.stage)
    if category == "MAX_STEPS":
        return "step_budget_exhausted"
    if "GOVERNANCE" in stage or "APPROVAL" in category or "POLICY" in category:
        return "policy_violation_rejected"
    if category == "DECISION_ERROR" or stage == "ADAPTER":
        return "goal_not_satisfied"
    if harness_result.validation_result == "FAIL":
        return "wrong_repair"
    if harness_result.termination_reason == "MODEL_FINISH":
        return "early_finish"
    return "goal_not_satisfied"


def run_deterministic_readiness(output_dir: Path) -> dict[str, Any]:
    """Exercise two real failing prefixes and four isolated canonical repair forks."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    experiment_id = "exp:phase4d-readiness:" + uuid.uuid4().hex
    checks: dict[str, dict[str, Any]] = {}
    all_trials = []
    pair_records = []

    def check(name: str, passed: bool, **facts: Any) -> None:
        checks[name] = {"passed": bool(passed), **facts}

    checkpoints = {}
    pairs = {}
    for task_id in ("behavior_repair", "verification_repair"):
        pair_id = f"pair:{task_id}:{uuid.uuid4().hex}"
        checkpoint = prepare_readiness_checkpoint(
            task_id, output_dir / task_id, experiment_id=experiment_id, pair_id=pair_id,
        )
        checkpoints[task_id] = checkpoint
        pair = {
            "experiment_id": experiment_id,
            "pair_id": pair_id,
            "task_id": task_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_fingerprint": checkpoint.fingerprint,
            "baseline_trial_id": f"trial:baseline:{uuid.uuid4().hex}",
            "treatment_trial_id": f"trial:treatment:{uuid.uuid4().hex}",
            "baseline_run_id": f"baseline-{uuid.uuid4().hex}",
            "treatment_run_id": f"treatment-{uuid.uuid4().hex}",
            "provider_config_fingerprint": checkpoint.payload["budgets"]["provider_configuration_fingerprint"],
            "step_budget": checkpoint.payload["budgets"]["steps_total"],
            "repair_budget": checkpoint.payload["budgets"]["repairs_total"],
        }
        pair_path = output_dir / task_id / "pairs" / f"{pair_id.replace(':', '-')}.json"
        _atomic_json(pair_path, pair)
        pairs[task_id] = pair
        pair_records.append(pair)

    for task_id, checkpoint in checkpoints.items():
        pair = pairs[task_id]
        baseline = _run_one_fork(
            checkpoint, task_id=task_id, condition="baseline", output_dir=output_dir / task_id,
            pair_id=pair["pair_id"], trial_id=pair["baseline_trial_id"], run_id=pair["baseline_run_id"],
        )
        treatment = _run_one_fork(
            checkpoint, task_id=task_id, condition="treatment", output_dir=output_dir / task_id,
            pair_id=pair["pair_id"], trial_id=pair["treatment_trial_id"], run_id=pair["treatment_run_id"],
        )
        pair["baseline_start_fingerprint"] = baseline["starting_workspace_fingerprint"]
        pair["treatment_start_fingerprint"] = treatment["starting_workspace_fingerprint"]
        pair["baseline_workspace_id"] = baseline["workspace_id"]
        pair["treatment_workspace_id"] = treatment["workspace_id"]
        pair["baseline_checkpoint_fingerprint"] = baseline["checkpoint_fingerprint"]
        pair["treatment_checkpoint_fingerprint"] = treatment["checkpoint_fingerprint"]
        # Re-render the exact captured first decision state by reading the
        # prompt hashes and projection captured by each branch's record below.
        baseline_trial_record = _find_trial_record(output_dir / task_id, baseline["trial_id"])
        treatment_trial_record = _find_trial_record(output_dir / task_id, treatment["trial_id"])
        prompts = (baseline_trial_record.get("initial_model_prompt"), treatment_trial_record.get("initial_model_prompt"))
        if prompts[0] is None or prompts[1] is None:
            # The deterministic agent records a prompt hash in the durable
            # record; full prompts are kept only in process memory below.
            prompt_check = {"consistent": False, "intervention_only_repair_context": False, "reason": "actual initial input was not captured"}
        else:
            prompt_check = compare_initial_inputs(*prompts)
        pair["first_input_comparison"] = prompt_check
        pair_records[-1] = pair
        _atomic_json(output_dir / task_id / "pairs" / f"{pair['pair_id'].replace(':', '-')}.json", pair)
        all_trials.extend((baseline, treatment))

    samples = [
        {
            "pair_id": item.get("pair_id"),
            "condition": item.get("condition"),
            "validity": item.get("validity"),
            "harness_success": item.get("harness_success"),
            "oracle_success": item.get("oracle_success"),
            "primary_success": item.get("primary_success"),
            "canonical_action_path_preserved": item.get("canonical_action_path_preserved"),
        }
        for item in all_trials
    ]
    summary = readiness_summary(samples)
    check("one_checkpoint_to_two_trials", all(
        sum(1 for item in all_trials if item["pair_id"] == pair["pair_id"] and item["checkpoint_id"] == pair["checkpoint_id"]) == 2
        for pair in pair_records
    ), pair_count=len(pair_records), trials=len(all_trials))
    check("starting_workspace_fingerprints_equal", all(
        pair.get("baseline_start_fingerprint") == pair.get("treatment_start_fingerprint") == checkpoints[pair["task_id"]].payload["workspace"]["content_fingerprint"]
        for pair in pair_records
    ))
    check("shared_checkpoint_identity_equal", all(
        pair.get("baseline_checkpoint_fingerprint") == pair.get("treatment_checkpoint_fingerprint") == pair["checkpoint_fingerprint"]
        for pair in pair_records
    ))
    input_results = [pair.get("first_input_comparison", {}) for pair in pair_records]
    check("first_model_input_intervention_only", all(item.get("consistent") and item.get("intervention_only_repair_context") for item in input_results), comparisons=input_results)
    check("repair_context_provenance_valid", all(
        validate_repair_provenance(
            checkpoints[task_id].restore_repair_context(),
            checkpoints[task_id].payload["repair_context_provenance"],
            validation_attempt=checkpoints[task_id].payload["validation_attempt"],
            contract=checkpoints[task_id].payload["contract"],
            producer_artifact_id=checkpoints[task_id].payload["producer"]["trial_id"],
        )
        for task_id in checkpoints
    ))
    check("oracle_post_run_only", all(
        _event_index(item, "HARNESS_TERMINATED") < _event_index(item, "FINAL_STATE_EXPORTED") < _event_index(item, "ORACLE_COMPLETED")
        for item in all_trials
    ))
    check("oracle_scores_exported_final_state", all(
        item.get("oracle", {}).get("source_final_fingerprint") == item.get("final_workspace_fingerprint")
        and item.get("oracle", {}).get("derived_workspace_fingerprint") == item.get("final_workspace_fingerprint")
        for item in all_trials
    ))
    persistence = _interruption_persistence_probe(output_dir / "persistence_probe")
    check("trial_persistence_survives_interruption", persistence)
    calls = [call for trial in all_trials for call in trial.get("provider_calls", ())]
    check("provider_calls_bound_to_trial_run_decision", _provider_call_bindings_valid(calls), provider_calls=len(calls), live_provider_used=False)
    check("verification_assertion_repair_lineage_valid", all(
        item.get("checkpoint_validation_attempt_id")
        and item.get("evidence_lineage")
        and all(entry.get("execution_result_ids") for entry in item["validation_attempts"])
        for item in all_trials
    ))
    classifier_checks = all(
        classify_sample({"timeout_observed": True, "evidence_complete": False}) is SampleValidity.UNDETERMINED
        and classify_sample({"provider_status": "rate_limited", "evidence_complete": True}) is SampleValidity.INVALID_EXTERNAL
        and classify_sample({"setup_checks": {"workspace_match": False}}) is SampleValidity.INVALID_SETUP
        and classify_sample({"model_outcome": "wrong_repair", "evidence_complete": True}) is SampleValidity.VALID
        for _ in range(2)
    )
    check("sample_classifier_deterministic", classifier_checks)
    check("zero_valid_pair_not_estimable", readiness_summary([])["effect_status"] == "NOT_ESTIMABLE")
    check("provider_input_provenance_manifest_complete", all(
        all(
            isinstance(field.get("section"), str)
            and isinstance(field.get("field"), str)
            and isinstance(field.get("trust_class"), str)
            and isinstance(field.get("rendered_sha256"), str)
            and (field.get("trust_class") == "COMPOSITE" or bool(field.get("source_references")))
            for field in call.get("model_visible_provenance", ())
        )
        for trial in all_trials for call in trial.get("provider_calls", ())
    ))
    false_success_block = readiness_summary([{
        "validity": "VALID", "harness_success": True, "oracle_success": False,
    }])
    check("false_success_blocks_readiness", false_success_block["readiness"] == "FAIL" and false_success_block["false_success_count"] == 1)

    readiness = (
        "NOT_PROVEN" if summary["readiness"] == "NOT_PROVEN"
        else "PASS" if all(value["passed"] for value in checks.values()) and summary["false_success_count"] == 0
        else "FAIL"
    )
    report = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "readiness": readiness,
        "task_count": len(checkpoints),
        "checkpoint_count": len(checkpoints),
        "trial_count": len(all_trials),
        "provider_call_count": len(calls),
        "real_model_trials_run": False,
        "canonical_action_path_preserved": all(item.get("canonical_action_path_preserved") is True for item in all_trials),
        "canonical_action_path_status": summary["canonical_action_path_status"],
        "effect_conclusion": "NOT_RUN",
        "effect_status": summary["effect_status"],
        "false_success_count": summary["false_success_count"],
        "pairs": pair_records,
        "trials": all_trials,
        "checks": checks,
        "runtime_core_changed": False,
        "action_v2_changed": False,
        "execution_result_changed": False,
        "validator_semantics_changed": False,
        "generated_at": _now(),
    }
    _atomic_json(output_dir / "readiness.json", report)
    return report


def _provider_trial_has_completed_decisions(trial: Mapping[str, Any]) -> bool:
    calls = trial.get("provider_calls", ())
    return bool(
        calls
        and all(call.get("canonical_action_id") for call in calls)
        and trial.get("status") == "CLASSIFIED"
        and trial.get("validity") == SampleValidity.VALID.value
        and trial.get("canonical_action_path_preserved") is True
    )


def _mark_pair_setup_invalid(output_dir: Path, trial: dict[str, Any], reason: str) -> dict[str, Any]:
    record = TrialRecord.load(
        Path(output_dir) / "trials" / f"{trial['trial_id'].replace(':', '-')}.json"
    )
    setup = dict(record.payload.get("setup_checks", {}))
    setup["first_provider_input_boundary"] = False
    record.update(
        validity=SampleValidity.INVALID_SETUP.value,
        setup_checks=setup,
        pair_input_mismatch=reason,
    )
    record.transition("CLASSIFIED", validity=SampleValidity.INVALID_SETUP.value, pair_input_mismatch=reason)
    return record.payload


def run_real_model_readiness(output_dir: Path) -> dict[str, Any]:
    """Run at most one no-retry, two-task real-model A/B readiness evaluation.

    This entry point is intentionally opt-in. Every provider decision has a
    bounded adapter timeout and persisted start facts; a decision that does not
    yield a canonical Action stops the run before another provider request.
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    experiment_id = "exp:phase4d-real-readiness:" + uuid.uuid4().hex
    checkpoints: dict[str, FailureCheckpoint] = {}
    pairs: dict[str, dict[str, Any]] = {}
    trial_records: list[dict[str, Any]] = []
    stop_reason = ""

    for task_id in ("behavior_repair", "verification_repair"):
        pair_id = f"pair:{task_id}:{uuid.uuid4().hex}"
        checkpoint = prepare_readiness_checkpoint(
            task_id, output_dir / task_id, experiment_id=experiment_id, pair_id=pair_id,
        )
        checkpoints[task_id] = checkpoint
        pair = {
            "experiment_id": experiment_id,
            "pair_id": pair_id,
            "task_id": task_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_fingerprint": checkpoint.fingerprint,
            "baseline_trial_id": f"trial:baseline:{uuid.uuid4().hex}",
            "treatment_trial_id": f"trial:treatment:{uuid.uuid4().hex}",
            "baseline_run_id": f"baseline-{uuid.uuid4().hex}",
            "treatment_run_id": f"treatment-{uuid.uuid4().hex}",
            "provider_config_fingerprint": checkpoint.payload["budgets"]["provider_configuration_fingerprint"],
            "step_budget": checkpoint.payload["budgets"]["steps_total"],
            "repair_budget": checkpoint.payload["budgets"]["repairs_total"],
        }
        _atomic_json(output_dir / task_id / "pairs" / f"{pair_id.replace(':', '-')}.json", pair)
        pairs[task_id] = pair

    for task_id, checkpoint in checkpoints.items():
        pair = pairs[task_id]
        conditions = (
            ("baseline", pair["baseline_trial_id"], pair["baseline_run_id"]),
            ("treatment", pair["treatment_trial_id"], pair["treatment_run_id"]),
        )
        branch_trials: dict[str, dict[str, Any]] = {}
        for condition, trial_id, run_id in conditions:
            try:
                trial = _run_one_fork(
                    checkpoint,
                    task_id=task_id,
                    condition=condition,
                    output_dir=output_dir / task_id,
                    pair_id=pair["pair_id"],
                    trial_id=trial_id,
                    run_id=run_id,
                    real_model=True,
                )
            except Exception as error:
                try:
                    trial = _find_trial_record(output_dir / task_id, trial_id)
                    persisted = TrialRecord.load(
                        output_dir / task_id / "trials" / f"{trial_id.replace(':', '-')}.json"
                    )
                    persisted.update(real_runner_error={
                        "error_type": type(error).__name__,
                        "detail": str(error)[:512],
                    })
                    trial = persisted.payload
                except (OSError, ValueError):
                    trial = {"trial_id": trial_id, "condition": condition, "validity": "UNDETERMINED"}
                branch_trials[condition] = trial
                trial_records.append(trial)
                stop_reason = f"{condition} trial stopped: {type(error).__name__}"
                break

            branch_trials[condition] = trial
            trial_records.append(trial)
            if not _provider_trial_has_completed_decisions(trial):
                stop_reason = (
                    f"{condition} trial did not produce a complete canonical provider decision sequence; "
                    "no further provider call was started"
                )
                break

        if stop_reason:
            pair["status"] = "INCOMPLETE"
            pair["stop_reason"] = stop_reason
            _atomic_json(output_dir / task_id / "pairs" / f"{pair['pair_id'].replace(':', '-')}.json", pair)
            break

        baseline = branch_trials["baseline"]
        treatment = branch_trials["treatment"]
        baseline_record = _find_trial_record(output_dir / task_id, baseline["trial_id"])
        treatment_record = _find_trial_record(output_dir / task_id, treatment["trial_id"])
        input_comparison = compare_provider_initial_inputs(
            baseline_record.get("provider_calls", ()), treatment_record.get("provider_calls", ()),
        )
        pair.update(
            status="COMPLETED",
            baseline_workspace_id=baseline.get("workspace_id"),
            treatment_workspace_id=treatment.get("workspace_id"),
            baseline_start_fingerprint=baseline.get("starting_workspace_fingerprint"),
            treatment_start_fingerprint=treatment.get("starting_workspace_fingerprint"),
            baseline_checkpoint_fingerprint=baseline.get("checkpoint_fingerprint"),
            treatment_checkpoint_fingerprint=treatment.get("checkpoint_fingerprint"),
            first_actual_provider_input_comparison=input_comparison,
        )
        if not input_comparison.get("consistent"):
            replacement = []
            for trial in (baseline, treatment):
                invalid = _mark_pair_setup_invalid(
                    output_dir / task_id, trial, str(input_comparison.get("reason", "A/B inputs differ")),
                )
                replacement.append(invalid)
                trial_records = [invalid if item.get("trial_id") == invalid.get("trial_id") else item for item in trial_records]
        _atomic_json(output_dir / task_id / "pairs" / f"{pair['pair_id'].replace(':', '-')}.json", pair)

    checks = {
        "one_checkpoint_to_two_trials": all(
            pair.get("baseline_trial_id") and pair.get("treatment_trial_id")
            and pair.get("checkpoint_id") == checkpoints[task_id].checkpoint_id
            for task_id, pair in pairs.items()
        ),
        "starting_workspace_fingerprints_equal": all(
            pair.get("status") == "COMPLETED"
            and pair.get("baseline_start_fingerprint") == pair.get("treatment_start_fingerprint")
                == checkpoints[task_id].payload["workspace"]["content_fingerprint"]
            for task_id, pair in pairs.items()
            if pair.get("status") == "COMPLETED"
        ),
        "shared_checkpoint_identity_equal": all(
            pair.get("baseline_checkpoint_fingerprint") == pair.get("treatment_checkpoint_fingerprint")
                == pair["checkpoint_fingerprint"]
            for pair in pairs.values() if pair.get("status") == "COMPLETED"
        ),
        "first_model_input_intervention_only": all(
            pair.get("first_actual_provider_input_comparison", {}).get("consistent") is True
            for pair in pairs.values() if pair.get("status") == "COMPLETED"
        ),
        "repair_context_provenance_valid": all(
            validate_repair_provenance(
                checkpoints[task_id].restore_repair_context(),
                checkpoints[task_id].payload["repair_context_provenance"],
                validation_attempt=checkpoints[task_id].payload["validation_attempt"],
                contract=checkpoints[task_id].payload["contract"],
                producer_artifact_id=checkpoints[task_id].payload["producer"]["trial_id"],
            ) for task_id in checkpoints
        ),
        "oracle_post_run_only": all(
            _event_index(item, "HARNESS_TERMINATED") < _event_index(item, "FINAL_STATE_EXPORTED")
            < _event_index(item, "ORACLE_COMPLETED")
            for item in trial_records if item.get("oracle") is not None
        ),
        "oracle_scores_exported_final_state": all(
            item.get("oracle", {}).get("source_final_fingerprint") == item.get("final_workspace_fingerprint")
            and item.get("oracle", {}).get("derived_workspace_fingerprint") == item.get("final_workspace_fingerprint")
            for item in trial_records if item.get("oracle") is not None
        ),
        "trial_persistence_survives_interruption": _interruption_persistence_probe(output_dir / "persistence_probe"),
        "provider_calls_bound_to_trial_run_decision": _provider_call_bindings_valid([
            call for item in trial_records for call in item.get("provider_calls", ())
        ]),
        "verification_assertion_repair_lineage_valid": all(
            item.get("evidence_lineage") and item.get("checkpoint_validation_attempt_id")
            for item in trial_records if item.get("status") == "CLASSIFIED"
        ),
        "sample_classifier_deterministic": (
            classify_sample({"timeout_observed": True, "evidence_complete": False}) is SampleValidity.UNDETERMINED
            and classify_sample({"provider_status": "rate_limited", "evidence_complete": True}) is SampleValidity.INVALID_EXTERNAL
            and classify_sample({"setup_checks": {"workspace_match": False}}) is SampleValidity.INVALID_SETUP
            and classify_sample({"model_outcome": "wrong_repair", "evidence_complete": True}) is SampleValidity.VALID
        ),
        "zero_valid_pair_not_estimable": readiness_summary([])["effect_status"] == "NOT_ESTIMABLE",
        "hidden_oracle_data_absent_from_model_input": all(
            call.get("repair_provenance_valid") is True
            for item in trial_records for call in item.get("provider_calls", ())
        ),
        "false_success_blocks_readiness": readiness_summary([{
            "validity": "VALID", "harness_success": True, "oracle_success": False,
        }])["readiness"] == "FAIL",
    }
    samples = [{
        "pair_id": item.get("pair_id"),
        "condition": item.get("condition"),
        "validity": item.get("validity"),
        "harness_success": item.get("harness_success"),
        "oracle_success": item.get("oracle_success"),
        "canonical_action_path_preserved": item.get("canonical_action_path_preserved"),
    } for item in trial_records]
    summary = readiness_summary(samples)
    complete_pair_count = sum(pair.get("status") == "COMPLETED" for pair in pairs.values())
    readiness = (
        summary["readiness"] if summary["readiness"] in {"NOT_PROVEN", "FAIL"}
        else "PASS" if complete_pair_count == len(checkpoints) and all(checks.values())
        and summary["readiness"] == "PASS"
        else "PARTIAL" if not stop_reason else "NOT_ESTIMABLE"
    )
    report = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "readiness": readiness,
        "task_count": len(checkpoints),
        "checkpoint_count": len(checkpoints),
        "trial_count": len(trial_records),
        "provider_call_count": sum(len(item.get("provider_calls", ())) for item in trial_records),
        "real_model_trials_run": bool(trial_records),
        "effect_conclusion": "NOT_RUN",
        "effect_status": summary["effect_status"],
        "canonical_action_path_status": summary["canonical_action_path_status"],
        "false_success_count": summary["false_success_count"],
        "stop_reason": stop_reason,
        "pairs": list(pairs.values()),
        "trials": trial_records,
        "checks": {name: {"passed": bool(value)} for name, value in checks.items()},
        "generated_at": _now(),
    }
    _atomic_json(output_dir / "real_model_readiness.json", report)
    return report


def _find_trial_record(root: Path, trial_id: str) -> dict[str, Any]:
    safe = trial_id.replace(":", "-")
    path = Path(root) / "trials" / f"{safe}.json"
    return TrialRecord.load(path).payload


def _event_index(trial: Mapping[str, Any], status: str) -> int:
    return next((index for index, item in enumerate(trial.get("events", ())) if item.get("status") == status), -1)


def _interruption_persistence_probe(directory: Path) -> bool:
    record = TrialRecord.create(
        Path(directory) / "interrupted.json", experiment_id="exp:interruption",
        pair_id="pair:interruption", checkpoint_id="checkpoint:interruption",
        trial_id="trial:interruption", run_id="run:interruption",
        condition="baseline",
    )
    record.transition("STARTED")
    record.provider_decision_started(decision_seq=1, provider_call_local_id="provider-call:pending")
    loaded = TrialRecord.load(record.path)
    call = loaded.payload["provider_calls"][0]
    return (
        loaded.payload["status"] == "PROVIDER_DECISION_STARTED"
        and call["boundary_timestamps"]["decision_started"] is not None
        and call["boundary_timestamps"]["action_parsed"] is None
        and call["native_tool_call_count"] is None
        and call["last_known_boundary"] == "decision_started"
    )


def _provider_call_bindings_valid(calls: Sequence[Mapping[str, Any]]) -> bool:
    return all(
        call.get("trial_id") and call.get("run_id") and int(call.get("decision_seq", 0)) > 0
        and call.get("provider_call_local_id") and call.get("checkpoint_id") and call.get("workspace_id")
        and call.get("model_input_sha256") and call.get("input_without_repair_sha256")
        and call.get("decision_schema_sha256") and call.get("provider_configuration_fingerprint")
        for call in calls
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--deterministic-only", action="store_true")
    mode.add_argument("--real-model", action="store_true",
                      help="explicitly run the bounded, no-retry real-model A/B readiness check")
    args = parser.parse_args()
    result = (
        run_real_model_readiness(args.output_dir)
        if args.real_model else run_deterministic_readiness(args.output_dir)
    )
    evidence_path = args.output_dir / ("real_model_readiness.json" if args.real_model else "readiness.json")
    print(json.dumps({
        "readiness": result["readiness"],
        "task_count": result["task_count"],
        "trial_count": result["trial_count"],
        "false_success_count": result["false_success_count"],
        "evidence": str(evidence_path),
    }, ensure_ascii=False))
    return 0 if result["readiness"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
