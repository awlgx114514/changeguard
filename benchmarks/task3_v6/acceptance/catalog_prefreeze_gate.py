from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from benchmarks.task3_v6.profile_catalog import (
    CoverageStage,
    ProfileAcceptance,
    ProfileCatalog,
    ProfileClass,
    evaluate_profile_coverage,
    load_profile_catalog,
)
from benchmarks.task3_v6.source_binding import file_sha256, source_tree_identity


PREFREEZE_RUN_COUNT = 8
_REPOSITORY = Path(__file__).resolve().parents[3]
_HELPER_PATHS = (
    _REPOSITORY / "benchmarks/task3_v6/profile_catalog.py",
    _REPOSITORY / "benchmarks/task3_v6/source_binding.py",
    _REPOSITORY / "benchmarks/task3_v6/campaign.py",
    _REPOSITORY / "benchmarks/task3_v6/measurement.py",
    _REPOSITORY / "benchmarks/task3_v6/acceptance/test_known_fix_resource_probe.py",
)


def _environment_identity(details: dict[str, Any]) -> str:
    encoded = json.dumps(details, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _asset_manifest(
    profile_id: str,
    catalog: ProfileCatalog,
) -> tuple[Path, dict[str, Any], list[str]] | None:
    entry = next(item for item in catalog.profiles if item.profile_id == profile_id)
    manifest_reference = entry.asset_registration.get("manifest_reference")
    asset_path = entry.asset_registration.get("path")
    if not manifest_reference or not asset_path:
        return None

    asset_root = (_REPOSITORY / asset_path).resolve()
    manifest_path = (_REPOSITORY / manifest_reference).resolve()
    if not asset_root.is_dir() or not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    binding_errors = []
    if manifest.get("asset_id") != profile_id:
        binding_errors.append("asset manifest profile identity mismatch")
    tree_hash, file_count = source_tree_identity(asset_root)
    if tree_hash != manifest.get("final_tree_hash"):
        binding_errors.append("asset source tree hash mismatch")
    if file_count != manifest.get("file_count"):
        binding_errors.append("asset source file count mismatch")
    if manifest.get("asset_identity") != f"sha256:{tree_hash}":
        binding_errors.append("asset identity does not match source tree")
    if manifest.get("fixture_identity") != f"sha256:{tree_hash}":
        binding_errors.append("fixture identity does not match source tree")
    if manifest.get("benchmark_implementation_commit") != "b92c883a567e4b1cd7af5c8b02d9a13363713a3b":
        binding_errors.append("asset manifest lacks frozen benchmark commit binding")
    if manifest.get("benchmark_implementation_tree") != "a0bc6855a8bb97e97d1dc459d98ca1d3b6a70553":
        binding_errors.append("asset manifest lacks frozen benchmark tree binding")
    if manifest.get("profile_catalog_version") != catalog.version:
        binding_errors.append("asset manifest profile catalog version mismatch")
    if manifest.get("profile_catalog_sha256") != catalog.sha256:
        binding_errors.append("asset manifest profile catalog hash mismatch")
    source_acceptance = manifest.get("source_acceptance", {})
    if (
        manifest.get("source_acceptance_status") != "PASS"
        and source_acceptance.get("git_tree_blob_comparison") != "PASS"
    ):
        binding_errors.append("asset source acceptance is not recorded as PASS")
    public_suite = source_acceptance.get("public_subprocess_suite")
    if entry.profile_class is ProfileClass.CORRECT:
        if public_suite != "54 passed, 34 skipped, 0 failed":
            binding_errors.append("source-bound public subprocess suite result is missing")
    elif entry.profile_class is ProfileClass.REQUIRED_WRONG:
        expected_failures = (
            public_suite.get("expected_failures")
            if isinstance(public_suite, dict)
            else None
        )
        counts = (
            public_suite.get("passed"),
            public_suite.get("skipped"),
            public_suite.get("failed"),
        ) if isinstance(public_suite, dict) else ()
        clean_public_suite = public_suite == "54 passed, 34 skipped, 0 failed"
        target_failures_only = (
            isinstance(public_suite, dict)
            and public_suite.get("result") == "TARGET_SEMANTIC_DEFECT_ONLY"
            and all(type(value) is int and value >= 0 for value in counts)
            and isinstance(expected_failures, list)
            and len(expected_failures) == public_suite.get("failed")
            and all(isinstance(value, str) and value for value in expected_failures)
            and public_suite.get("unexpected_failures") == []
        )
        if not clean_public_suite and not target_failures_only:
            binding_errors.append(
                "wrong-profile public subprocess result is neither clean nor target-only"
            )
        defect = manifest.get("registered_lifecycle_defect")
        if (
            not isinstance(defect, dict)
            or defect.get("interaction_type") != "public_close"
            or defect.get("required_fact") != "DIRECT_CHILD_LIVE_CONFIRMED"
        ):
            binding_errors.append("wrong-profile lifecycle defect registration is missing")
    return manifest_path, manifest, binding_errors


def _buffering_spooling_failure_reasons(
    probe: Any,
    *,
    expected_backend_path: str,
    expected_backend_sha256: str,
) -> list[str]:
    if not isinstance(probe, dict) or probe.get("status") != "PASS":
        return ["finite-output buffering and spooling probe did not pass"]

    expected_payload = bytes.fromhex(probe["expected_payload_hex"])
    public_prefix = bytes.fromhex(probe["public_prefix_hex"])
    buffered_bytes = bytes.fromhex(probe["pre_close_buffer_hex"])
    spooled_bytes = bytes.fromhex(probe["spool_write_hex"])
    reasons = []
    if public_prefix + buffered_bytes != expected_payload:
        reasons.append("finite-output bytes are incomplete before spooling")
    if spooled_bytes != buffered_bytes:
        reasons.append("spool write differs from the pending bytes")
    if probe.get("spool_returned_bytes") != len(buffered_bytes):
        reasons.append("spool write count differs from the pending bytes")
    if probe.get("spool_closed") is not True:
        reasons.append("spooled temporary file ownership was not closed")
    if probe.get("direct_child_returncode") != 0:
        reasons.append("finite-output direct child did not complete successfully")
    if probe.get("public_stream_after_close") != "CLOSED_RESOURCE_ERROR":
        reasons.append("public close behavior did not close the same stream")
    if (
        probe.get("stream_identity_before")
        != probe.get("stream_identity_after")
    ):
        reasons.append("public stream identity changed across close")
    if (
        probe.get("pipe_transport_identity_before")
        != probe.get("pipe_transport_identity_after")
    ):
        reasons.append("stdout pipe transport identity changed across close")
    if probe.get("pipe_transport_closed_after") is not True:
        reasons.append("original stdout pipe transport remains open after close")
    if (
        probe.get("backend_path_before") != expected_backend_path
        or probe.get("backend_path_after") != expected_backend_path
        or probe.get("backend_sha256_before") != expected_backend_sha256
        or probe.get("backend_sha256_after") != expected_backend_sha256
    ):
        reasons.append("post-probe AnyIO source identity differs from the registered asset")
    return reasons


def _record_failure_reasons(
    record: dict[str, Any],
    *,
    profile_id: str,
    profile_class: ProfileClass,
    manifest_path: Path,
    manifest: dict[str, Any],
    manifest_binding_errors: list[str],
    catalog: ProfileCatalog,
) -> list[str]:
    reasons = list(manifest_binding_errors)
    source = record.get("source_import_evidence", {})
    lifecycle = record.get("lifecycle_evidence", {})
    completion = record.get("completion_evidence", {})
    attribution = record.get("attribution_result", {})
    manifest_hash = file_sha256(manifest_path)
    entry = next(item for item in catalog.profiles if item.profile_id == profile_id)
    expected_source_root = str(
        (_REPOSITORY / entry.asset_registration["path"] / "src").resolve(strict=True)
    )
    expected_backend_path = str(
        Path(expected_source_root) / "anyio" / "_backends" / "_asyncio.py"
    )
    expected_backend_hash = manifest.get("file_sha256", {}).get(
        "src/anyio/_backends/_asyncio.py"
    )

    if record.get("artifact_schema") != "task3-profile-prefreeze-run-v1":
        reasons.append("unknown run evidence schema")
    if record.get("profile_id") != profile_id:
        reasons.append("profile identity mismatch")
    if record.get("profile_class") != profile_class.value:
        reasons.append("profile class mismatch")
    if record.get("benchmark_implementation_commit") != manifest["benchmark_implementation_commit"]:
        reasons.append("benchmark implementation commit mismatch")
    if record.get("benchmark_implementation_tree") != manifest["benchmark_implementation_tree"]:
        reasons.append("benchmark implementation tree mismatch")
    if record.get("profile_catalog_version") != catalog.version:
        reasons.append("profile catalog version mismatch")
    if record.get("profile_catalog_sha256") != catalog.sha256:
        reasons.append("profile catalog semantic hash mismatch")
    if record.get("asset_manifest_path") != manifest_path.relative_to(_REPOSITORY).as_posix():
        reasons.append("asset manifest path differs from the catalog")
    if record.get("asset_manifest_sha256") != manifest_hash:
        reasons.append("asset manifest hash mismatch")
    if record.get("asset_identity") != manifest.get("asset_identity"):
        reasons.append("asset identity mismatch")
    if record.get("fixture_identity") != manifest.get("fixture_identity"):
        reasons.append("fixture identity mismatch")
    if record.get("asset_tree_sha256") != manifest.get("final_tree_hash"):
        reasons.append("asset source tree mismatch")
    if record.get("asset_file_count") != manifest.get("file_count"):
        reasons.append("asset source file count mismatch")
    if record.get("environment_identity") != _environment_identity(
        record.get("environment_details", {})
    ):
        reasons.append("environment identity hash mismatch")
    required_environment_fields = {
        "os",
        "python_executable",
        "python_version",
        "dependencies",
        "pytest_plugins",
        "path",
        "cwd",
    }
    if not required_environment_fields.issubset(
        record.get("environment_details", {})
    ):
        reasons.append("environment identity details are incomplete")
    if source.get("binding_status") != "PASS":
        reasons.append("source import binding failed")
    if source.get("actual_anyio_path") != str(Path(expected_source_root) / "anyio" / "__init__.py"):
        reasons.append("actual AnyIO import path mismatch")
    if source.get("actual_backend_path") != expected_backend_path:
        reasons.append("actual backend import path mismatch")
    if source.get("actual_backend_sha256") != expected_backend_hash:
        reasons.append("actual backend import hash mismatch")
    if record.get("actual_backend_sha256") != expected_backend_hash:
        reasons.append("recorded backend hash mismatch")
    if record.get("helper_identity") != {
        path.relative_to(_REPOSITORY).as_posix(): file_sha256(path)
        for path in _HELPER_PATHS
    }:
        reasons.append("grader or helper source hash mismatch")
    if record.get("grader_sha256") != file_sha256(
        _REPOSITORY / "benchmarks/task3_v6/acceptance/test_known_fix_resource_probe.py"
    ):
        reasons.append("grader hash mismatch")
    if lifecycle.get("evidence_id") not in completion.get("lifecycle_evidence_ids", []):
        reasons.append("lifecycle evidence is not referenced by completion evidence")
    if record.get("completion_evidence", {}).get("observation_state") != "COMMITTED":
        reasons.append("completion evidence is not committed")
    if completion.get("completion_kind") != "returned":
        reasons.append("completion did not return normally")
    if completion.get("semantic_timeout") != "NOT_CONFIRMED":
        reasons.append("deadline evidence does not confirm bounded completion")
    if lifecycle.get("observation_source") != "SUPERVISOR_TRUSTED_OS":
        reasons.append("lifecycle observation is not supervisor-owned")
    if lifecycle.get("query_order_status") != "VALID_FIRST_EXTERNAL_CHECK":
        reasons.append("lifecycle observation order is invalid")
    if lifecycle.get("child_binding_status") != "VALID":
        reasons.append("direct child identity binding is invalid")
    if record.get("raw_wait_result") != lifecycle.get("os_query_raw_result"):
        reasons.append("raw wait result differs from lifecycle evidence")
    if (
        completion.get("campaign_id") != record.get("campaign_id")
        or completion.get("slot_id") != record.get("slot_id")
        or completion.get("run_id") != record.get("run_id")
        or completion.get("asset_identity") != record.get("asset_identity")
        or completion.get("environment_identity") != record.get("environment_identity")
    ):
        reasons.append("completion evidence identity differs from the current run")
    if (
        lifecycle.get("campaign_id") != record.get("campaign_id")
        or lifecycle.get("slot_id") != record.get("slot_id")
        or lifecycle.get("run_id") != record.get("run_id")
        or lifecycle.get("asset_identity") != record.get("asset_identity")
        or lifecycle.get("interaction_id") != completion.get("interaction_id")
        or lifecycle.get("transaction_identity") != completion.get("record_commit_identity")
        or lifecycle.get("transaction_completion_identity") != completion.get("snapshot_identity")
    ):
        reasons.append("lifecycle evidence identity differs from completion evidence")
    if completion.get("evidence_id") not in record.get("evidence_ids", []):
        reasons.append("terminal decision does not reference completion evidence")
    if lifecycle.get("evidence_id") not in record.get("evidence_ids", []):
        reasons.append("terminal decision does not reference lifecycle evidence")
    if record.get("terminal_decision", {}).get("verdict") != record.get("task_verdict"):
        reasons.append("terminal decision verdict differs from the recorded task verdict")

    component_verdicts = [
        item.get("verdict") for item in record.get("component_results", [])
    ]
    if profile_class is ProfileClass.CORRECT:
        if record.get("task_verdict") != "PASS":
            reasons.append("correct profile task verdict is not PASS")
        if not component_verdicts or any(value != "PASS" for value in component_verdicts):
            reasons.append("correct profile contains a non-PASS component")
        if lifecycle.get("os_query_raw_result") != 0:
            reasons.append("direct child remained live or had invalid OS status")
        if record.get("public_stream_probe", {}).get("status") != "CLOSED_RESOURCE_ERROR":
            reasons.append("public stream probe did not report ClosedResourceError")
        if profile_id == "CA_BUFFERING_SPOOLING":
            reasons.extend(
                _buffering_spooling_failure_reasons(
                    record.get("buffering_spooling_probe"),
                    expected_backend_path=expected_backend_path,
                    expected_backend_sha256=expected_backend_hash,
                )
            )
    else:
        if record.get("task_verdict") != "FAIL":
            reasons.append("wrong profile task verdict is not FAIL")
        if record.get("fail_attribution") != "confirmed_rejection":
            reasons.append("wrong profile lacks trusted confirmed rejection")
        if attribution.get("valid") is not True or attribution.get("category") != "confirmed_rejection":
            reasons.append("wrong profile rejection attribution is invalid")
        if not component_verdicts or "FAIL" not in component_verdicts:
            reasons.append("wrong profile has no failed component")
        if any(value in {"INVALID", "NOT_PROVEN"} for value in component_verdicts):
            reasons.append("wrong profile contains invalid or unproven components")

    return reasons


def evaluate_catalog_prefreeze(
    catalog: ProfileCatalog,
    evidence_root: str | Path,
    *,
    expected_run_count: int = PREFREEZE_RUN_COUNT,
) -> dict[str, Any]:
    if expected_run_count != PREFREEZE_RUN_COUNT:
        raise ValueError("Task3 v6 prefreeze requires exactly eight fresh runs per profile")

    root = Path(evidence_root).resolve()
    acceptances = []
    profile_runs: dict[str, dict[str, Any]] = {}
    for entry in catalog.profiles:
        if entry.profile_class is ProfileClass.EXCLUDED_LIMITATION:
            continue

        profile_root = root / entry.profile_id
        manifest_result = _asset_manifest(entry.profile_id, catalog)
        if manifest_result is None:
            profile_runs[entry.profile_id] = {
                "status": "ASSET_OR_MANIFEST_MISSING",
                "run_count": 0,
                "failure_reasons": ["catalog has no registered source manifest"],
            }
            continue

        manifest_path, manifest, manifest_binding_errors = manifest_result
        records = []
        artifact_refs = []
        for path in sorted(profile_root.rglob("*.json")) if profile_root.exists() else ():
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("artifact_schema") != "task3-profile-prefreeze-run-v1":
                continue
            records.append(record)
            artifact_refs.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": file_sha256(path),
                }
            )

        reasons = list(manifest_binding_errors)
        if len(records) != expected_run_count:
            reasons.append(
                f"expected {expected_run_count} fresh run records, found {len(records)}"
            )
        run_ids = [record.get("run_id") for record in records]
        campaign_ids = [record.get("campaign_id") for record in records]
        if len(run_ids) != len(set(run_ids)):
            reasons.append("run identifiers are not unique")
        if len(campaign_ids) != len(set(campaign_ids)):
            reasons.append("campaign identifiers are not unique")
        environment_ids = {record.get("environment_identity") for record in records}
        if len(environment_ids) != 1:
            reasons.append("run environments differ within the prefreeze group")
        for record in records:
            reasons.extend(
                f"{record.get('run_id')}: {reason}"
                for reason in _record_failure_reasons(
                    record,
                    profile_id=entry.profile_id,
                    profile_class=entry.profile_class,
                    manifest_path=manifest_path,
                    manifest=manifest,
                    manifest_binding_errors=manifest_binding_errors,
                    catalog=catalog,
                )
            )

        evidence_identity = hashlib.sha256(
            json.dumps(artifact_refs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        group_id = f"prefreeze-{entry.profile_id}-{evidence_identity[:16]}"
        source_reference = f"catalog-prefreeze:{entry.profile_id}:sha256:{evidence_identity}"
        accepted = len(records) == expected_run_count and not reasons
        if records:
            acceptances.append(
                ProfileAcceptance(
                    campaign_id=group_id,
                    profile_id=entry.profile_id,
                    profile_class=entry.profile_class,
                    catalog_version=catalog.version,
                    catalog_hash=catalog.sha256,
                    stage=CoverageStage.PREFREEZE,
                    admission=("PREFREEZE_ACCEPTED" if accepted else "PREFREEZE_NOT_ACCEPTED"),
                    source_reference=source_reference,
                )
            )

        profile_runs[entry.profile_id] = {
            "status": "PREFREEZE_ACCEPTED" if accepted else "PREFREEZE_NOT_ACCEPTED",
            "run_count": len(records),
            "pass_count": sum(record.get("task_verdict") == "PASS" for record in records),
            "fail_count": sum(record.get("task_verdict") == "FAIL" for record in records),
            "invalid_count": sum(record.get("task_verdict") == "INVALID" for record in records),
            "not_proven_count": sum(
                record.get("task_verdict") == "NOT_PROVEN" for record in records
            ),
            "confirmed_rejection_count": sum(
                record.get("fail_attribution") == "confirmed_rejection"
                and record.get("attribution_result", {}).get("valid") is True
                for record in records
            ),
            "environment_identity": next(iter(environment_ids), None),
            "artifact_references": artifact_refs,
            "failure_reasons": sorted(set(reasons)),
            "acceptance_identity": source_reference if accepted else None,
        }

    coverage = evaluate_profile_coverage(
        catalog,
        acceptances,
        CoverageStage.PREFREEZE,
    )
    return {
        "coverage": coverage,
        "profile_runs": profile_runs,
        "profile_acceptances": acceptances,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    catalog = load_profile_catalog()
    evaluated = evaluate_catalog_prefreeze(catalog, args.evidence_root)
    summary = {
        "benchmark_implementation_commit": "b92c883a567e4b1cd7af5c8b02d9a13363713a3b",
        "benchmark_implementation_tree": "a0bc6855a8bb97e97d1dc459d98ca1d3b6a70553",
        "profile_catalog_version": catalog.version,
        "profile_catalog_sha256": catalog.sha256,
        "prefreeze_gate_sha256": file_sha256(Path(__file__).resolve()),
        "coverage": asdict(evaluated["coverage"]),
        "profile_runs": evaluated["profile_runs"],
        "profile_acceptances": [asdict(item) for item in evaluated["profile_acceptances"]],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(summary, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    print(
        json.dumps(
            {
                "coverage_status": evaluated["coverage"].coverage_status,
                "correct_completed": evaluated["coverage"].required_correct_completed,
                "correct_total": evaluated["coverage"].required_correct_total,
                "wrong_completed": evaluated["coverage"].required_wrong_completed,
                "wrong_total": evaluated["coverage"].required_wrong_total,
                "output": str(args.output.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0 if evaluated["coverage"].coverage_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
