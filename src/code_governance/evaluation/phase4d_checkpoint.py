"""Versioned, integrity-checked snapshots at the validation repair boundary."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Mapping

from code_governance.context.models import (
    AgentContext,
    ContextFile,
    MemoryContext,
    StrategyContext,
)
from code_governance.memory.retriever import TaskContext
from code_governance.repo.analyzer import RepoContext
from code_governance.validator.evidence import WorkspaceIdentity
from code_governance.validator.repair_context import (
    RepairFailureContext,
    repair_failure_identity,
)
from code_governance.runtime.action import Action
from code_governance.runtime.history import HistoryManager
from code_governance.runtime.observation import Observation
from code_governance.runtime.state import AgentState


SCHEMA_VERSION = 1
_REQUIRED = {
    "schema_version", "checkpoint_id", "checkpoint_fingerprint", "artifact_root",
    "experiment_id", "pair_id", "task", "fixture", "workspace", "producer",
    "contract", "plan", "context", "agent_state", "validation_attempt",
    "repair_context", "repair_context_provenance", "budgets", "artifact_hashes",
}
_HASHED_FIELDS = (
    "task", "fixture", "workspace", "producer", "contract", "plan", "context",
    "agent_state", "validation_attempt", "repair_context",
    "repair_context_provenance", "budgets",
)
_FORBIDDEN_KEYS = {
    "hidden_oracle", "reference_patch", "gold_patch", "solution_patch",
    "manual_fix_hint", "evaluation_repair_suggestion",
}


class CheckpointError(ValueError):
    """A checkpoint is incomplete, corrupted, or outside its declared boundary."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise CheckpointError(f"checkpoint contains a non-JSON value: {error}") from error


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def workspace_manifest(root: Path) -> list[dict[str, Any]]:
    root = Path(root).resolve()
    if not root.is_dir():
        raise CheckpointError(f"workspace snapshot is missing: {root}")
    manifest = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise CheckpointError(f"workspace snapshot contains an unsupported symlink: {relative}")
        if path.is_dir():
            manifest.append({"path": relative, "kind": "directory"})
        elif path.is_file():
            content = path.read_bytes()
            manifest.append({
                "path": relative,
                "kind": "file",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
    return manifest


def workspace_fingerprint(root: Path) -> str:
    return digest_json(workspace_manifest(root))


def _reject_forbidden_keys(value: Any, path: str = "checkpoint") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS:
                raise CheckpointError(f"forbidden solution/oracle field at {path}.{key}")
            _reject_forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_forbidden_keys(item, f"{path}[{index}]")


@dataclass(frozen=True)
class FailureCheckpoint:
    """Frozen facts sufficient to fork exactly one failed validation boundary.

    This deliberately is not a generic Runtime serialization. Its strict schema
    contains only the task, context, state and evidence needed by repair_start.
    """

    payload: dict[str, Any]

    @classmethod
    def create(cls, payload: Mapping[str, Any]) -> "FailureCheckpoint":
        data = dict(payload)
        data["schema_version"] = SCHEMA_VERSION
        data.pop("checkpoint_id", None)
        data.pop("checkpoint_fingerprint", None)
        data.pop("artifact_hashes", None)
        data["artifact_hashes"] = {key: digest_json(data[key]) for key in _HASHED_FIELDS}
        data["checkpoint_fingerprint"] = digest_json(data)
        data["checkpoint_id"] = "checkpoint:" + data["checkpoint_fingerprint"][:24]
        checkpoint = cls(data)
        checkpoint.validate()
        return checkpoint

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FailureCheckpoint":
        if not isinstance(value, Mapping):
            raise CheckpointError("checkpoint root must be an object")
        keys = set(value)
        if keys != _REQUIRED:
            missing = sorted(_REQUIRED - keys)
            extra = sorted(keys - _REQUIRED)
            raise CheckpointError(f"checkpoint schema mismatch; missing={missing}, extra={extra}")
        checkpoint = cls(dict(value))
        checkpoint.validate()
        return checkpoint

    @classmethod
    def load(cls, path: Path) -> "FailureCheckpoint":
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CheckpointError(f"unable to load checkpoint: {error}") from error
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, Any]:
        # Round-tripping also prevents callers mutating nested checkpoint facts.
        return json.loads(canonical_json(self.payload))

    @property
    def checkpoint_id(self) -> str:
        return str(self.payload["checkpoint_id"])

    @property
    def fingerprint(self) -> str:
        return str(self.payload["checkpoint_fingerprint"])

    @property
    def snapshot_path(self) -> Path:
        return Path(self.payload["workspace"]["snapshot_path"]).resolve()

    def workspace_fingerprint(self, root: Path) -> str:
        return workspace_fingerprint(root)

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return path

    def validate(self) -> None:
        data = self.payload
        if set(data) != _REQUIRED:
            raise CheckpointError("checkpoint contains fields outside the versioned schema")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise CheckpointError("unsupported FailureCheckpoint schema version")
        _reject_forbidden_keys(data)
        if not all(isinstance(data.get(key), str) and data[key] for key in ("experiment_id", "pair_id")):
            raise CheckpointError("checkpoint experiment and pair identities are required")
        if data.get("artifact_hashes") != {key: digest_json(data[key]) for key in _HASHED_FIELDS}:
            raise CheckpointError("checkpoint artifact hash mismatch")
        body = dict(data)
        recorded = body.pop("checkpoint_fingerprint", None)
        body.pop("checkpoint_id", None)
        if recorded != digest_json(body):
            raise CheckpointError("checkpoint fingerprint mismatch")
        if data.get("checkpoint_id") != "checkpoint:" + str(recorded)[:24]:
            raise CheckpointError("checkpoint identity does not match its fingerprint")

        attempt = data["validation_attempt"]
        state = data["agent_state"]
        budgets = data["budgets"]
        if attempt.get("status") != "FAIL" or not attempt.get("failure_contexts"):
            raise CheckpointError("checkpoint must reference a real repairable validation failure")
        producer_run_id = data["producer"].get("run_id")
        if (
            not producer_run_id
            or producer_run_id != attempt.get("producer_run_id")
            or producer_run_id != state.get("run_id")
        ):
            raise CheckpointError("producer run identity does not match failure evidence and AgentState")
        if data["producer"].get("harness_validation_attempt_id") != attempt.get("attempt_id"):
            raise CheckpointError("producer validation-attempt identity does not match checkpoint evidence")
        if state.get("status") != "FAILED" or state.get("validation_result") != "FAIL":
            raise CheckpointError("checkpoint AgentState is not at a failed validation boundary")
        if digest_json(state.get("contract", {})) != digest_json(data["contract"]):
            raise CheckpointError("contract identity does not match AgentState")
        if (
            state.get("task_description") != data["task"].get("description")
            or data["context"].get("task_context", {}).get("task_text") != data["task"].get("description")
        ):
            raise CheckpointError("task identity does not match Context and AgentState")
        if list(data["workspace"].get("changed_files", ())) != list(attempt.get("changed_files", ())):
            raise CheckpointError("checkpoint changed-file evidence does not match ValidationAttempt")
        if budgets.get("steps_consumed") != state.get("step_count"):
            raise CheckpointError("checkpoint consumed step budget does not match AgentState")
        if not (0 <= budgets.get("steps_consumed", -1) <= budgets.get("steps_total", -1)):
            raise CheckpointError("checkpoint step budget is invalid")
        if not (0 <= budgets.get("repairs_consumed", -1) < budgets.get("repairs_total", 0)):
            raise CheckpointError("checkpoint has no remaining repair budget")
        if data["context"].get("repair_context"):
            raise CheckpointError("prefix Context must not already contain the repair intervention")

        root = Path(data["artifact_root"]).resolve()
        snapshot = self.snapshot_path
        try:
            snapshot.relative_to(root)
        except ValueError as error:
            raise CheckpointError("workspace snapshot is outside the checkpoint artifact root") from error
        manifest = workspace_manifest(snapshot)
        if manifest != data["workspace"].get("manifest"):
            raise CheckpointError("checkpoint workspace manifest mismatch")
        if digest_json(manifest) != data["workspace"].get("content_fingerprint"):
            raise CheckpointError("checkpoint workspace fingerprint mismatch")
        self._validate_producer_consistency(snapshot)

    def _validate_producer_consistency(self, snapshot: Path) -> None:
        data = self.payload
        attempt = data["validation_attempt"]
        state = data["agent_state"]
        contract_digest = digest_json(data["contract"])
        if digest_json(data["context"].get("contract")) != contract_digest:
            raise CheckpointError("restored Context Contract identity differs from canonical Contract")
        context = data["context"]
        if any(context.get(key) for key in (
            "memory_context", "strategies", "working_memory", "active_issues", "decisions",
        )):
            raise CheckpointError("checkpoint Context contains unproven model-visible dynamic input")
        if data["task"].get("description") != context.get("task_context", {}).get("task_text"):
            raise CheckpointError("checkpoint task Context is not the declared public task input")
        if state.get("task_description") != data["task"].get("description"):
            raise CheckpointError("checkpoint Runtime task is not the declared public task input")

        attempt_id = str(attempt.get("attempt_id", ""))
        producer_run_id = str(data["producer"].get("run_id", ""))
        if not attempt_id.startswith(producer_run_id + ":validation:"):
            raise CheckpointError("producer validation attempt is not present in its run identity")

        try:
            with tempfile.TemporaryDirectory(
                prefix="checkpoint-audit-index-", dir=Path(data["artifact_root"]).resolve(),
            ) as directory:
                audit_index = Path(directory) / "index"
                source_index = snapshot / ".git" / "index"
                if source_index.is_file():
                    shutil.copyfile(source_index, audit_index)
                env = os.environ.copy()
                env["GIT_INDEX_FILE"] = str(audit_index)
                revision = _git_text(snapshot, "rev-parse", "HEAD", env=env).strip()
                fingerprint = _git_visible_fingerprint(
                    snapshot, revision, Path(data["artifact_root"]).resolve(),
                )
                patch = _git_text(snapshot, "diff", "--binary", "HEAD", env=env)
                actual_changed = _canonical_paths(
                    _git_text(snapshot, "diff", "--name-status", "HEAD", env=env),
                    _git_text(snapshot, "ls-files", "--others", "--exclude-standard", env=env),
                )
            changes = _canonical_paths_from_evidence(attempt)
        except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
            raise CheckpointError(f"unable to verify producer workspace evidence: {error}") from error

        if revision != data["fixture"].get("baseline_revision"):
            raise CheckpointError("producer workspace revision differs from frozen fixture baseline")
        identity = attempt.get("workspace_identity", {})
        if (
            identity.get("revision_before") != revision
            or identity.get("revision_after") != revision
            or identity.get("fingerprint_before") != fingerprint
            or identity.get("fingerprint_after") != fingerprint
        ):
            raise CheckpointError("restored workspace identity differs from producer ValidationAttempt")
        if actual_changed != changes or actual_changed != _canonical_path_list(data["workspace"].get("changed_files", ())):
            raise CheckpointError("workspace changed-file set differs from producer ValidationAttempt")
        if hashlib.sha256(patch.encode("utf-8")).hexdigest() != data["workspace"].get("prefix_patch_fingerprint"):
            raise CheckpointError("prefix patch fingerprint differs from restored producer workspace")

        action_ids = [str(item.get("id", "")) for item in state.get("recent_actions", ())]
        observation_ids = [str(item.get("action_id", "")) for item in state.get("recent_observations", ())]
        attempt_action_ids = [str(item) for item in attempt.get("action_ids", ())]
        if (
            not action_ids
            or any(not item for item in action_ids + observation_ids)
            or len(set(action_ids)) != len(action_ids)
            or action_ids != observation_ids
            or action_ids != attempt_action_ids
        ):
            raise CheckpointError("producer Action/Observation history does not match ValidationAttempt lineage")

        trace_path = snapshot / ".agent" / "runs" / producer_run_id / "trace.json"
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CheckpointError("producer execution Trace is missing or unreadable") from error
        trace_events = trace.get("events", ()) if isinstance(trace, Mapping) else ()
        if not isinstance(trace_events, list) or trace.get("run_id") != producer_run_id:
            raise CheckpointError("producer execution Trace identity is invalid")
        traced = {
            event_type: [
                str(item.get("data", {}).get("action_id", ""))
                for item in trace_events if item.get("type") == event_type
            ]
            for event_type in ("action_decided", "action_executed", "observation_received")
        }
        if (
            traced["action_decided"] != action_ids
            or traced["action_executed"] != action_ids
            or traced["observation_received"] != action_ids
            or int(state.get("step_count", -1)) != len(action_ids)
            or len(action_ids) > int(state.get("max_steps", 0))
        ):
            raise CheckpointError("producer Action/Observation trajectory is incomplete or inconsistent with Trace")

        replay = AgentState.initialize(
            data["task"]["description"], run_id=producer_run_id,
            max_steps=int(state.get("max_steps", 10)),
        )
        history = HistoryManager()
        for action_payload, observation_payload in zip(
            state.get("recent_actions", ()), state.get("recent_observations", ()),
        ):
            history.record(
                replay,
                Action.from_dict(action_payload),
                Observation.from_dict(observation_payload),
            )
        if [item.to_dict() for item in replay.working_memory_summary] != state.get("working_memory_summary", ()):
            raise CheckpointError("checkpoint Working Memory is not derived from producer Observations")
        if replay.decisions != state.get("decisions", ()):
            raise CheckpointError("checkpoint decisions are not marked historical Action text")
        issue_projection = lambda items: sorted(
            (item.get("type"), item.get("description"), item.get("status")) for item in items
        )
        if issue_projection([item.to_dict() for item in replay.issues]) != issue_projection(state.get("issues", ())):
            raise CheckpointError("checkpoint Active Issues are not derived from producer execution facts")
        if attempt.get("repair_issue_descriptions", ()) != _repair_issue_descriptions(attempt):
            raise CheckpointError("checkpoint Active Issue descriptions are not derived from producer Validation evidence")

        failures = tuple(attempt.get("failure_contexts", ()))
        checkpoint_failures = tuple(data.get("repair_context", ()))
        if not failures or failures != checkpoint_failures:
            raise CheckpointError("checkpoint failure context identity differs from producer ValidationAttempt")
        findings = tuple(attempt.get("findings", ()))
        assertion_results = {item.get("assertion_id"): item for item in attempt.get("assertion_results", ())}
        verification = {item.get("claim_id"): item for item in attempt.get("verification_evidence", ())}
        for failure in failures:
            expected_id = repair_failure_identity(
                attempt_id,
                identity.get("fingerprint_after"),
                failure.get("change_id"),
                failure.get("failure_boundary"),
                failure.get("assertion_id"),
                failure.get("claim_id"),
            )
            if failure.get("failure_id") != expected_id:
                raise CheckpointError("shared failure identity is not derived from producer validation evidence")
            code = "ASSERTION_FAILED" if failure.get("failure_boundary") == "ASSERTION" else "verification_failed"
            if not any(item.get("code") == code and item.get("severity") == "error" for item in findings):
                raise CheckpointError("producer validation failure identity is absent from ValidationAttempt findings")
            if failure.get("assertion_id"):
                result = assertion_results.get(failure["assertion_id"])
                if not result or result.get("status") != "failed":
                    raise CheckpointError("producer failure assertion is absent from ValidationAttempt evidence")
            if failure.get("claim_id") and failure["claim_id"] not in verification:
                raise CheckpointError("producer failure claim is absent from ValidationAttempt evidence")
        runtime_failure = state.get("failure") or {}
        if state.get("status") != "FAILED" or runtime_failure.get("stage") != "VALIDATION":
            raise CheckpointError("producer terminal failure is not the checkpoint validation boundary")
        self._validate_producer_binding(snapshot)

    def _validate_producer_binding(self, snapshot: Path) -> None:
        """Bind the projection to facts retained from the actual producer run.

        The existing prefix trial artifact is the independent producer record;
        neither checkpoint resealing nor rewriting its copied Trace updates it.
        """
        data = self.payload
        producer = data["producer"]
        root = Path(data["artifact_root"]).resolve()
        trial_id = str(producer.get("trial_id", ""))
        prefix = "trial:prefix:"
        nonce = trial_id[len(prefix):] if trial_id.startswith(prefix) else ""
        if len(nonce) != 32 or any(character not in "0123456789abcdef" for character in nonce):
            raise CheckpointError("producer trial artifact identity is invalid")
        artifact = root / "trials" / (trial_id.replace(":", "-") + ".json")
        try:
            record = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CheckpointError("independent producer trial artifact is missing or unreadable") from error
        if not isinstance(record, Mapping) or any(
            record.get(key) != expected for key, expected in (
                ("trial_id", trial_id), ("run_id", producer["run_id"]),
                ("experiment_id", data["experiment_id"]), ("pair_id", data["pair_id"]),
                ("task_id", data["task"]["id"]), ("condition", "prefix"),
            )
        ):
            raise CheckpointError("independent producer trial identity does not match checkpoint")
        truth = record.get("producer_truth")
        if not isinstance(truth, Mapping):
            raise CheckpointError("independent producer canonical facts are missing")
        # These inputs were captured from the Runtime/Harness result before
        # checkpoint creation, rather than hashed from the submitted projection.
        for key in ("agent_state", "validation_attempt", "contract", "plan", "context"):
            if key not in truth or digest_json(data[key]) != digest_json(truth[key]):
                raise CheckpointError(f"checkpoint {key} differs from canonical producer facts")
        canonical_state = truth["agent_state"]
        accounting = (
            (producer.get("decision_seq"), truth.get("decision_seq")),
            (producer.get("configured_prefix_repairs"), truth.get("configured_prefix_repairs")),
            (data["budgets"].get("steps_consumed"), canonical_state.get("step_count")),
            (data["budgets"].get("repairs_consumed"), truth.get("repairs_consumed")),
            (data["budgets"].get("steps_total"), truth.get("steps_total")),
            (data["budgets"].get("repairs_total"), truth.get("repairs_total")),
        )
        if any(expected is None or digest_json(actual) != digest_json(expected) for actual, expected in accounting):
            raise CheckpointError("checkpoint accounting differs from canonical producer accounting")
        workspace = data["workspace"]
        if (
            str(snapshot) != truth.get("workspace_snapshot_path")
            or workspace.get("content_fingerprint") != truth.get("workspace_content_fingerprint")
            or workspace.get("prefix_patch_fingerprint") != truth.get("prefix_patch_fingerprint")
        ):
            raise CheckpointError("checkpoint workspace differs from canonical producer workspace")
        relative_trace = Path(".agent") / "runs" / producer["run_id"] / "trace.json"
        try:
            original_trace = (root / "p" / "repo" / relative_trace).read_bytes()
            copied_trace = (snapshot / relative_trace).read_bytes()
        except OSError as error:
            raise CheckpointError("independent producer execution Trace is missing or unreadable") from error
        if any(
            hashlib.sha256(content).hexdigest() != truth.get("trace_sha256")
            for content in (original_trace, copied_trace)
        ):
            raise CheckpointError("checkpoint Trace differs from canonical producer execution Trace")

    def restore_workspace(self, destination: Path) -> str:
        self.validate()
        destination = Path(destination).resolve()
        if destination.exists():
            raise CheckpointError(f"restore destination must not exist: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.snapshot_path, destination)
        restored = workspace_fingerprint(destination)
        expected = self.payload["workspace"]["content_fingerprint"]
        if restored != expected:
            shutil.rmtree(destination, ignore_errors=True)
            raise CheckpointError("restored workspace fingerprint mismatch")
        return restored

    def validate_baseline(self, repository: Path) -> None:
        repository = Path(repository).resolve()
        expected = self.payload["fixture"]
        try:
            revision = __import__("subprocess").run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        except (OSError, __import__("subprocess").CalledProcessError) as error:
            raise CheckpointError(f"unable to verify trial baseline identity: {error}") from error
        if revision != expected.get("baseline_revision"):
            raise CheckpointError("trial baseline revision differs from the FailureCheckpoint")
        if _tracked_content_fingerprint(repository) != expected.get("baseline_content_fingerprint"):
            raise CheckpointError("trial baseline content differs from the FailureCheckpoint")

    def restore_context(self, repository_root: Path) -> AgentContext:
        value = self.payload["context"]
        repository = value["repository_context"]
        task = value["task_context"]
        return AgentContext(
            version=str(value["version"]),
            task_context=TaskContext(
                task_text=str(task["task_text"]), task_type=str(task["task_type"]),
                target_files=tuple(task["target_files"]), modules=tuple(task["modules"]),
                keywords=tuple(task["keywords"]),
            ),
            repository_context=RepoContext(
                root=Path(repository_root), files=tuple(repository["files"]),
                python_modules=tuple(repository["python_modules"]),
                git_status=tuple(repository["git_status"]), core_files=tuple(repository["core_files"]),
                module_relationships={key: tuple(items) for key, items in repository["module_relationships"].items()},
            ),
            memory_context=tuple(MemoryContext(**item) for item in value["memory_context"]),
            project_rules=tuple(value["project_rules"]),
            relevant_files=tuple(ContextFile(**item) for item in value["relevant_files"]),
            constraints=tuple(value["constraints"]),
            strategies=tuple(StrategyContext(**item) for item in value["strategies"]),
            available_tools=tuple(value["available_tools"]),
            available_operations=tuple(dict(item) for item in value["available_operations"]),
            current_goal=str(value.get("current_goal", "")),
            working_memory=tuple(value.get("working_memory", ())),
            active_issues=tuple(value.get("active_issues", ())),
            decisions=tuple(value.get("decisions", ())),
            contract=value.get("contract"),
            repair_context=(),
        )

    def restore_repair_context(self) -> tuple[RepairFailureContext, ...]:
        result = []
        for item in self.payload["repair_context"]:
            identity = item["workspace_identity"]
            result.append(RepairFailureContext(
                failure_id=str(item["failure_id"]), change_id=str(item["change_id"]),
                failure_boundary=str(item["failure_boundary"]),
                assertion_id=item.get("assertion_id"), claim_id=item.get("claim_id"),
                expected=item.get("expected"), observed=item.get("observed"),
                evidence_refs=tuple(item["evidence_refs"]), changed_files=tuple(item["changed_files"]),
                workspace_identity=WorkspaceIdentity(**identity),
                changed_files_omitted=int(item.get("changed_files_omitted", 0)),
            ))
        return tuple(result)


def _git_text(repository: Path, *args: str, env=None) -> str:
    command_env = os.environ.copy()
    command_env["GIT_OPTIONAL_LOCKS"] = "0"
    if env is not None:
        command_env.update(env)
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
        env=command_env,
    ).stdout


def _repair_issue_descriptions(attempt: Mapping[str, Any]) -> list[str]:
    descriptions = []
    verification = attempt.get("verification_evidence", ())
    for finding in attempt.get("findings", ()):
        if finding.get("severity") != "error":
            continue
        description = f"{finding.get('code', '')}: {finding.get('message', '')}"
        if finding.get("code") in {"verification_failed", "verification_execution_failed"}:
            failures = []
            for item in verification:
                if item.get("passed"):
                    continue
                metadata = item.get("execution_metadata", {})
                output = str(
                    metadata.get("stdout_preview")
                    or metadata.get("stderr_preview")
                    or ""
                ).strip()[:2000]
                failures.append(
                    "command=" + " ".join(str(part) for part in item.get("argv", ()))
                    + f"; exit={item.get('actual_result', {}).get('returncode')}; output={output}"
                )
            if failures:
                description += "\n" + "\n".join(failures)
        descriptions.append(description[:512])
    return descriptions


def _git_visible_fingerprint(repository: Path, revision: str, temporary_root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(revision.encode("utf-8"))
    digest.update(b"\0tracked\0")
    with tempfile.TemporaryDirectory(prefix="checkpoint-index-", dir=temporary_root) as directory:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(Path(directory) / "index")
        _git_text(repository, "read-tree", revision, env=env)
        digest.update(_git_text(repository, "diff", "--binary", revision, env=env).encode("utf-8"))
        digest.update(b"\0untracked\0")
        untracked = _git_text(repository, "ls-files", "--others", "--exclude-standard", env=env)
        for relative in sorted(path for path in untracked.splitlines() if path):
            path = repository / relative
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def _canonical_paths(name_status: str, untracked: str) -> tuple[str, ...]:
    paths = set()
    for line in name_status.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            paths.add(fields[-1].replace("\\", "/"))
    paths.update(path.replace("\\", "/") for path in untracked.splitlines() if path)
    return _canonical_path_list(paths)


def _canonical_path_list(paths) -> tuple[str, ...]:
    normalized = tuple(sorted(str(path).replace("\\", "/") for path in paths))
    if any(not path or path.startswith("/") or ".." in Path(path).parts for path in normalized):
        raise CheckpointError("producer changed-file evidence contains a non-canonical path")
    if len(set(normalized)) != len(normalized):
        raise CheckpointError("producer changed-file evidence contains duplicate paths")
    return normalized


def _canonical_paths_from_evidence(attempt: Mapping[str, Any]) -> tuple[str, ...]:
    changed = _canonical_path_list(attempt.get("changed_files", ()))
    context = _canonical_path_list(item.get("path", "") for item in attempt.get("change_context", ()))
    if changed != context:
        raise CheckpointError("ValidationAttempt changed files differ from its ChangeContext")
    return changed


def _tracked_content_fingerprint(repository: Path) -> str:
    import subprocess

    try:
        paths = subprocess.run(
            ["git", "-C", str(repository), "ls-files", "-z"],
            check=True, capture_output=True,
        ).stdout.split(b"\0")
        manifest = []
        for raw in paths:
            if not raw:
                continue
            relative = raw.decode("utf-8")
            content = (repository / Path(relative)).read_bytes()
            manifest.append({"path": relative.replace("\\", "/"), "sha256": hashlib.sha256(content).hexdigest()})
    except (OSError, UnicodeDecodeError, subprocess.CalledProcessError) as error:
        raise CheckpointError(f"unable to fingerprint trial baseline: {error}") from error
    return digest_json(manifest)
